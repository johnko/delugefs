#!/usr/bin/env python

APP_VERSION='0.4-dev'

'''
DelugeFS - A shared-nothing distributed filesystem built using Python, Bittorrent and Zeroconf.
            You'll need another setup such as Syncthing or a git cluster to sync the meta data.

Copyright (C) 2013  Derek Anderson
Copyright (C) 2015  John Ko  git@johnko.ca

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

import os, errno, sys, collections, uuid, shutil, traceback, random, select, time, socket, multiprocessing, stat, datetime, statvfs, math, BaseHTTPServer, SocketServer, hashlib
from threading import Thread, Lock
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn
import libtorrent
import pybonjour
from webapi import WebUIServer, WebUIHandler

SECONDS_TO_NEXT_CHECK = 10 # 120
FS_ENCODE = sys.getfilesystemencoding()
if not FS_ENCODE: FS_ENCODE = 'utf-8'

'''
Global variables for pybonjour callbacks
'''
resolved = []
queried = []

'''
Peer
----
This class represents a peer object. many peers will be initialized during normal use
'''
class Peer(object):
    def __init__(self, service_name, host, addr=None, bt_port=4433, datacenter=None):
        self.service_name = service_name
        self.host = host
        self.addr = addr
        self.bt_port = bt_port # SSL Torrent
        self.datacenter = datacenter

'''
DelugeFS
----
This class translates the mounted FUSE fs calls to perform torrent creation, downloading, etc.
If not mounted, it will not translate FUSE calls and just store/serve file data and metadata
'''
class DelugeFS(LoggingMixIn, Operations):
    def __init__(self, name, root, datacenter, bt_start_port, webip, webport, webdir, loglevel, lazy, create=False):
        self.name = name    # cluster name
        self.bj_name = self.name+'__'+uuid.uuid4().hex    # mDNS name
        if webport is not None:
            self.httpd = WebUIServer((webip, webport), WebUIHandler)
            print 'WebUIServer listening on: http://%s:%d/' % (webip, webport)
        else:
            self.httpd = type('blank', (object,), {})()
        self.httpd.api = {'webdir':webdir,
                'webip':webip,
                'webport':str(webport),
                'lazy':str(lazy),
                'name':name,
                'btport':str(bt_start_port),
                'root':root,
                'servicename':self.bj_name,
                'hostname':socket.gethostname(),
                'datacenter':datacenter,
                'mount':'-',
                'freespace': 0,
                'cluster_freespace': 0,
                'version':'v%s' % APP_VERSION
                }
        self.httpd.bt_handles = {}
        self.httpd.peers = {}
        self.httpd.nametoaddr = {}
        self.bootstrapping = True
        self.LOGLEVEL = loglevel
        self.lazy = lazy

        self.root = os.path.realpath(root)
        self.indexdir = os.path.join(self.root, 'meta', 'index').encode(FS_ENCODE)
        self.metadir = os.path.join(self.root, 'meta').encode(FS_ENCODE)
        self.chunksdir = os.path.join(self.root, 'chunks').encode(FS_ENCODE)
        self.tmp = os.path.join(self.root, 'tmp').encode(FS_ENCODE)
        if not os.path.isdir(self.root):
            os.mkdir(self.root)

        self.next_time_to_check_for_undermirrored_files = datetime.datetime.now() + datetime.timedelta(0,10+random.randint(0,30))
        self.last_read_file = {}

        self.bt_in_progress = set()
        self.bt_session = libtorrent.session()
        self.bt_session.listen_on(bt_start_port, bt_start_port+10)
        pe_settings = libtorrent.pe_settings()
        pe_enc_policy = {0:libtorrent.enc_policy.forced, 1:libtorrent.enc_policy.enabled, 2:libtorrent.enc_policy.disabled}
        pe_settings.out_enc_policy = libtorrent.enc_policy(pe_enc_policy[0])
        pe_settings.in_enc_policy = libtorrent.enc_policy(pe_enc_policy[0])
        pe_enc_level = {0:libtorrent.enc_level.plaintext, 1:libtorrent.enc_level.rc4, 2:libtorrent.enc_level.both}
        pe_settings.allowed_enc_level = libtorrent.enc_level(pe_enc_level[1])
        self.bt_session.set_pe_settings(pe_settings)
        self.bt_port = self.bt_session.listen_port()
        self.httpd.api['btport'] = self.bt_port

        # no libtorrent lsd for private if we use h.connect_peer
        self.bt_session.start_lsd()
        # self.bt_session.stop_lsd()

        # no libtorrent dht for private if we use h.connect_peer
        # self.bt_session.start_dht()
        self.bt_session.stop_dht()

        print 'libtorrent listening on:', self.bt_port
        # self.bt_session.add_dht_router('localhost', 10670)
        print '...dht_state()', self.bt_session.dht_state()

        thread = Thread(target=self.__start_webui)
        thread.daemon = True
        thread.start()
        thread = Thread(target=self.__bonjour_start_listening)
        thread.daemon = True
        thread.start()
        print 'give me a sec to look for other peers...'
        time.sleep(2)

        cnfn = os.path.join(self.metadir, '__delugefs__', 'cluster_name').encode(FS_ENCODE)
        if create:
            if os.listdir(self.root):
                files = [x for x in os.listdir(self.root) if x!=".git.meta" and x!="chunks" and x!="meta" and x!="tmp" ]
                if files:
                    raise Exception('--create specified, but %s is not empty' % self.root)
            if self.httpd.peers:
                raise Exception('--create specified, but i found %i peer%s using --id "%s" already' % (len(self.httpd.peers), 's' if len(self.httpd.peers)>1 else '', self.name))

            if not os.path.isdir(self.metadir): os.makedirs(self.metadir)
            if not os.path.isdir(os.path.dirname(cnfn)): os.mkdir(os.path.dirname(cnfn))
            with open(cnfn, 'w') as f:
                f.write(self.name)

            if not os.path.isdir(self.indexdir): os.makedirs(self.indexdir)
            with open(os.path.join(self.indexdir, '.__delugefs_dir__').encode(FS_ENCODE),'w') as f:
                f.write("git doesn't track empty dirs, so we add this file.")
        else:
            if os.path.isfile(cnfn):
                with open(cnfn, 'r') as f:
                    existing_cluster_name = f.read().strip()
                    if existing_cluster_name != self.name:
                        raise Exception('a cluster root exists at %s, but its name is "%s", not "%s"' % (self.root, existing_cluster_name, self.name))
            else:
                if os.listdir(self.root):
                    raise Exception('root %s is not empty, but no cluster was found' % self.root)
                if not self.httpd.peers:
                    raise Exception('--create not specified, no repo exists at %s and no peers of cluster "%s" found' % (self.root, self.name))
                if not os.path.isdir(self.metadir):
                    raise Exception('no repo exists at %s' % self.metadir)

        prune_empty_dirs(self.metadir)

        print '='*80

        if not os.path.isdir(self.tmp): os.makedirs(self.tmp)
        for fn in os.listdir(self.tmp): os.remove(os.path.join(self.tmp,fn).encode(FS_ENCODE))
        if not os.path.isdir(self.chunksdir): os.makedirs(self.chunksdir)

        self.rwlock = Lock()
        self.open_files = {} # used to track opened files except READONLY
        self.bootstrapping = False

        thread = Thread(target=self.__bonjour_register, args=())
        thread.daemon = True
        thread.start()
        thread = Thread(target=self.__load_local_torrents)
        thread.daemon = True
        thread.start()
        thread = Thread(target=self.__monitor)
        thread.daemon = True
        thread.start()

    '''
    Internal functions, alphabetical
    '''
    def __add_peer(self, service_name, host, addr, bt_port):
        print 'adding peer'
        if not servicename in self.httpd.peers:
            self.httpd.peers[servicename] = Peer(servicename, host, addr, bt_port)

    def __add_torrent(self, torrent, path):
        uid = torrent['info']['name']
        info = libtorrent.torrent_info(torrent)
        dat_file = os.path.join(self.chunksdir, uid[:2], uid).encode(FS_ENCODE)
        if not os.path.isdir(os.path.dirname(dat_file)): os.mkdir(os.path.dirname(dat_file))
        if not os.path.isfile(dat_file):
            with open(dat_file,'wb') as f:
                pass
        h = self.bt_session.add_torrent({'ti':info, 'save_path':os.path.dirname(dat_file)})
        for peer in self.httpd.peers.values():
            if (peer.bt_port is not None) and (self.httpd.nametoaddr[peer.host] is not None):
                if self.LOGLEVEL > 3: print 'adding peer:', (self.httpd.nametoaddr[peer.host], peer.bt_port)
                h.connect_peer((self.httpd.nametoaddr[peer.host], peer.bt_port), 0)
        if self.LOGLEVEL > 3: print 'added', uid
        self.httpd.bt_handles[path] = h
        self.bt_in_progress.add(path)

    def __bonjour_browse_callback(self, sdRef, flags, interfaceIndex, errorCode, serviceName, regtype, replyDomain):
        if errorCode != pybonjour.kDNSServiceErr_NoError:
            return
        if not (flags & pybonjour.kDNSServiceFlagsAdd):
            if serviceName in self.httpd.peers:
                del self.httpd.peers[serviceName]
            print 'self.httpd.peers', self.httpd.peers
            return
        resolve_sdRef = pybonjour.DNSServiceResolve(0, interfaceIndex, serviceName, regtype, replyDomain, self.__bonjour_resolve_callback)
        try:
            while not resolved:
                ready = select.select([resolve_sdRef], [], [], 5)
                if resolve_sdRef not in ready[0]:
                    #print 'Resolve timed out'
                    break
                pybonjour.DNSServiceProcessResult(resolve_sdRef)
            else:
                resolved.pop()
        finally:
            resolve_sdRef.close()

    def __bonjour_register_callback(self, sdRef, flags, errorCode, name, regtype, domain):
        if errorCode == pybonjour.kDNSServiceErr_NoError:
            print '...bonjour listener', name+'.'+regtype+domain, 'now listening on', self.bt_port

    def __bonjour_resolve_callback(self, sdRef, flags, interfaceIndex, errorCode, fullname, hosttarget, port, txtRecord):
        if errorCode == pybonjour.kDNSServiceErr_NoError:
            if fullname.startswith(self.bj_name):
                #print 'ignoring my own service'
                return
            if not (fullname.startswith(self.name+'__') and ('._delugefs._tcp.' in fullname )):
                #print 'ignoring unrelated service', fullname
                return
            servicename = fullname[:fullname.index('.')]
            print 'bonjour resolve found peer', servicename
            if not servicename in self.httpd.peers:
                self.httpd.peers[servicename] = Peer(servicename, hosttarget)
            if '._delugefs._tcp.' in fullname:
                self.httpd.peers[servicename].bt_port = port
            print 'self.httpd.peers', self.httpd.peers
            query_sdRef = pybonjour.DNSServiceQueryRecord(interfaceIndex = interfaceIndex,
                                                            fullname = hosttarget,
                                                            rrtype = pybonjour.kDNSServiceType_A,
                                                            callBack = self.__bonjour_query_record_callback)
            try:
                while not queried:
                    ready = select.select([query_sdRef], [], [], 5)
                    if query_sdRef not in ready[0]:
                        #print 'Query timed out'
                        break
                    pybonjour.DNSServiceProcessResult(query_sdRef)
                else:
                    queried.pop()
            finally:
                query_sdRef.close()
            resolved.append(True)

    def __bonjour_query_record_callback(self, sdRef, flags, interfaceIndex, errorCode, fullname, rrtype, rrclass, rdata, ttl):
        if errorCode == pybonjour.kDNSServiceErr_NoError:
            if self.LOGLEVEL > 3: print '  IP         =', socket.inet_ntoa(rdata)
            if self.LOGLEVEL > 3: print 'fullname ', fullname
            self.httpd.nametoaddr[fullname] = socket.inet_ntoa(rdata)
            if fullname in self.httpd.nametoaddr:
                self.httpd.nametoaddr[fullname] = socket.inet_ntoa(rdata)
                queried.append(True)
                return 'pulling from new peer', fullname

    def __bonjour_register(self):
        print 'registering bonjour listener...'
        bjservice = pybonjour.DNSServiceRegister(name=self.bj_name, regtype="_delugefs._tcp",
                        port=self.bt_port, callBack=self.__bonjour_register_callback)
        try:
            while True:
                ready = select.select([bjservice], [], [])
                if bjservice in ready[0]:
                    pybonjour.DNSServiceProcessResult(bjservice)
        except KeyboardInterrupt:
            pass

    def __bonjour_start_listening(self):
        browse_sdRef = pybonjour.DNSServiceBrowse(regtype="_delugefs._tcp", callBack=self.__bonjour_browse_callback)
        try:
            try:
                while True:
                    ready = select.select([browse_sdRef], [], [])
                    if browse_sdRef in ready[0]:
                        pybonjour.DNSServiceProcessResult(browse_sdRef)
            except KeyboardInterrupt:
                    pass
        finally:
            browse_sdRef.close()

    def __check_for_undermirrored_files(self):
        if self.lazy: return
        if self.next_time_to_check_for_undermirrored_files > datetime.datetime.now(): return
        try:
            if self.LOGLEVEL > 2: print 'check_for_undermirrored_files @', datetime.datetime.now()
            my_uids = set(self.__get_active_info_hashes())
            counter = collections.Counter(my_uids)
            peer_freespace = {'__self__': self.__get_freespace()}
            uid_peers = collections.defaultdict(set)
            for uid in my_uids:
                uid_peers[uid].add('__self__')
            for peer_id, peer in self.httpd.peers.items():
                for s in self.__get_peer_active_info_hashes(peer_id, peer.host, peer.datacenter):
                    counter[s] += 1
                    uid_peers[s].add(peer_id)
                # read from meta/info/servicename/freespace
                peer_freespace[peer_id] = self.__get_peer_freespace(peer_id)
            if self.LOGLEVEL > 2: print 'counter', counter
            if self.LOGLEVEL > 3: print 'peer_freespace', peer_freespace
            if len(self.httpd.peers.items()) < 1:
                if self.LOGLEVEL > 3: print "can't do anything, since i'm the only peer!"
                return
            self.httpd.api['cluster_freespace'] = '%0.2fGB' % (sum(peer_freespace.values()) / math.pow(2,30))
            if self.LOGLEVEL > 3: print 'cluster_freespace: %s' % self.httpd.api['cluster_freespace']
            for root, dirs, files in os.walk(self.indexdir):
                #print 'root, dirs, files', root, dirs, files
                if root.startswith(os.path.join(self.indexdir, '__delugefs__').encode(FS_ENCODE)): continue
                for fn in files:
                    if fn=='.__delugefs_dir__': continue
                    fn = os.path.join(root, fn).encode(FS_ENCODE)
                    e = get_torrent_dict(fn)
                    if not e:
                        if self.LOGLEVEL > 2: print 'not a torrent?', fn
                        continue
                    uid = e['info']['name']
                    size = e['info']['length']
                    path = fn[len(self.indexdir):]
                    if counter[uid] < 2:
                        peer_freespace_list = sorted([x for x in peer_freespace.items() if x[0] not in uid_peers[uid]], lambda x,y: x[1]<y[1])
                        if self.LOGLEVEL > 2: print 'peer_freespace_list', peer_freespace_list
                        for best_peer_id, freespace in peer_freespace_list:
                            if uid in my_uids and best_peer_id=='__self__':
                                best_peer_id = peer_freespace_list[1][0]
                            peer_freespace[best_peer_id] -= size
                            if self.LOGLEVEL > 2: print 'need to rep', path #, 'to', best_peer_id
                            if '__self__'==best_peer_id:
                                self.__please_mirror(path)
                            break
                    elif counter[uid] > 3:
                        if self.LOGLEVEL > 2: print 'uid_peers', uid_peers
                        peer_freespace_list = sorted([x for x in peer_freespace.items() if x[0] in uid_peers[uid]], lambda x,y: x[1]>y[1])
                        if self.LOGLEVEL > 2: print 'peer_freespace_list2', peer_freespace_list
                        for best_peer_id, freespace in peer_freespace_list:
                            if '__self__'==best_peer_id:
                                if self.__please_stop_mirroring(path): break
                        if self.LOGLEVEL > 2: print '__please_stop_mirroring', path, 'sent to'#, best_peer_id
        except Exception as e:
            traceback.print_exc()
        self.next_time_to_check_for_undermirrored_files = datetime.datetime.now() + datetime.timedelta(0,SECONDS_TO_NEXT_CHECK+random.randint(0,10*(1+len(self.httpd.peers))))

    def __finalize(self, path, olduid):
        if self.LOGLEVEL > 2: print 'finalize', path, olduid
        try:
            # try sha256 before making torrent
            old_tmp_fn = os.path.join(self.tmp, olduid).encode(FS_ENCODE)
            uid = sha256sum(old_tmp_fn, 4096)
            tmp_fn = os.path.join(self.tmp, uid).encode(FS_ENCODE)
            path = None
            if self.LOGLEVEL > 3: print 'olduid',olduid
            for k,v in self.open_files.items():
                if self.LOGLEVEL > 3: print 'k',k
                if self.LOGLEVEL > 3: print 'v',v
                if olduid.strip() == v:
                    if self.LOGLEVEL > 3: print 'found k',k
                    path = k
            if self.LOGLEVEL > 3: print 'path',path
            if self.LOGLEVEL > 3: print 'old_tmp',old_tmp_fn
            if self.LOGLEVEL > 3: print 'sha_tmp_fn',tmp_fn
            os.rename(old_tmp_fn, tmp_fn)
            fs = libtorrent.file_storage()
            #print tmp_fn
            libtorrent.add_files(fs, tmp_fn)
            t = libtorrent.create_torrent(fs)
            t.set_creator("DelugeFS");
            libtorrent.set_piece_hashes(t, self.tmp)
            tdata = t.generate()
            #print tdata
            fn = os.path.join(self.indexdir, path).encode(FS_ENCODE)
            with open(fn, 'wb') as f:
                f.write(libtorrent.bencode(tdata))
            #print 'wrote', fn
            dat_dir = os.path.join(self.chunksdir, uid[:2]).encode(FS_ENCODE)
            if not os.path.isdir(dat_dir):
                try: os.mkdir(dat_dir)
                except: pass
            shutil.copyfile(tmp_fn, os.path.join(dat_dir, uid).encode(FS_ENCODE))
            os.remove(tmp_fn)
            #print 'committing', fn
            self.__add_torrent(tdata, path)
            del self.open_files[path]
        except Exception as e:
            traceback.print_exc()
            raise e

    def __get_active_info_hashes(self):
        self.next_time_to_check_for_undermirrored_files = datetime.datetime.now() + datetime.timedelta(0,SECONDS_TO_NEXT_CHECK+random.randint(0,10*(1+len(self.httpd.peers))))
        active_info_hashes = []
        for k,h in self.httpd.bt_handles.items():
            if not h: continue
            try:
                active_info_hashes.append(str(h.get_torrent_info().name()))
            except:
                traceback.print_exc()
                del self.httpd.bt_handles[k]
        if self.LOGLEVEL > 2: print 'active_info_hashes', active_info_hashes
        return active_info_hashes

    def __get_peer_active_info_hashes(self, servicename, host, datacenter):
        # TODO read from metadir/datacenter/host/servicename/active
        active_info_hashes = []
        return active_info_hashes

    def __get_freespace(self):
        f = os.statvfs(self.root)
        bsize = f[statvfs.F_BSIZE]
        if bsize > 4096: bsize = 512
        freebytes = (bsize * f[statvfs.F_BFREE])
        # read, then write to only if different
        fn = os.path.join(self.metadir, 'info', self.bj_name, 'freespace').encode(FS_ENCODE)
        if not os.path.isdir(os.path.dirname(fn)): os.makedirs(os.path.dirname(fn))
        old_freebytes = 0
        if os.path.isfile(fn):
            with open(fn, 'r') as f:
                old_freebytes = f.read().strip()
        if old_freebytes != freebytes:
            with open(fn,'w') as f:
                f.write("%i" % freebytes)
        self.httpd.api['freespace'] = '%0.2fGB' % (freebytes / math.pow(2,30))
        return freebytes

    def __get_peer_freespace(self, servicename):
        # very simply read from the info
        fn = os.path.join(self.metadir, 'info', servicename, 'freespace').encode(FS_ENCODE)
        if not os.path.isdir(os.path.dirname(fn)): os.makedirs(os.path.dirname(fn))
        old_freebytes = 0
        if os.path.isfile(fn):
            with open(fn, 'r') as f:
                try:
                    old_freebytes = int(f.read().strip())
                except:
                    pass
        return old_freebytes

    def __load_local_torrents(self):
        #print 'self.indexdir', self.indexdir
        for root, dirs, files in os.walk(self.indexdir):
            #print 'root, dirs, files', root, dirs, files
            if root.startswith(os.path.join(self.indexdir, '__delugefs__').encode(FS_ENCODE)): continue
            for fn in files:
                if fn=='.__delugefs_dir__': continue
                fn = os.path.join(root, fn).encode(FS_ENCODE)
                if self.LOGLEVEL > 3: print 'loading torrent', fn
                e = get_torrent_dict(fn)
                if not e:
                    print 'not able to read torrent', fn
                else:
                    uid = e['info']['name']
                    info = libtorrent.torrent_info(e)
                    dat_file = os.path.join(self.chunksdir, uid[:2], uid).encode(FS_ENCODE)
                    #print 'dat_file', dat_file
                    if os.path.isfile(dat_file):
                        if not os.path.isdir(os.path.dirname(dat_file)): os.mkdir(os.path.dirname(dat_file))
                        h = self.bt_session.add_torrent({'ti':info, 'save_path':os.path.dirname(dat_file)})
                        if self.LOGLEVEL > 3: print 'added ', fn, '(%s)'%uid
                        self.httpd.bt_handles[fn[len(self.indexdir):]] = h
        if self.LOGLEVEL > 3: print 'self.httpd.bt_handles', self.httpd.bt_handles

    def __monitor(self):
        while True:
            time.sleep(random.randint(3,7))
            #print '='*80
            #TODO remove self.__write_active_torrents()
            self.__check_for_undermirrored_files()

    def __please_mirror(self, path):
        try:
            if self.LOGLEVEL > 3: print '__please_mirror', path
            fn = os.path.join(self.indexdir, path).encode(FS_ENCODE)
            torrent = get_torrent_dict(fn)
            if torrent:
                self.__add_torrent(torrent, path)
                return True
            else:
                return False
        except:
            traceback.print_exc()
            return False

    def __please_stop_mirroring(self, path):
        try:
            print 'got __please_stop_mirroring', path
            if path in self.last_read_file:
                if (datetime.datetime.now()-self.last_read_file[path]).seconds < 60*60*6:
                    print 'reject - too soon since we last used it', path
                    return False
            print 'i would have stopped', path
            return False
            # THIS DELETES THE CHUNK
            h = self.httpd.bt_handles[path]
            if h:
                uid = h.get_torrent_info().name()
                self.bt_session.remove_torrent(h)
                fn = os.path.join(self.chunksdir, uid[:2], uid).encode(FS_ENCODE)
                if os.path.isfile(fn): os.remove(fn)
                print 'stopped mirroring', path
                return True
            return False
        except:
            traceback.print_exc()
            return False

    def __start_webui(self):
        try:
            self.httpd.serve_forever()
        except:
            # not bound to a port because webui is disabled
            pass

    def __write_active_torrents(self):
        try:
            with open(os.path.join(self.metadir, '__delugefs__', 'active_torrents').encode(FS_ENCODE), 'w') as f:
                for path, h in self.httpd.bt_handles.items():
                    s = h.status()
                    state_str = ['queued', 'checking', 'downloading metadata', 'downloading', 'finished', 'seeding', 'allocating', 'checking resume data']
                    f.write('%s %s is %.2f%% complete (down: %.1f kb/s up: %.1f kB/s peers: %d) %s\n' % \
                            (path, h.get_torrent_info().name(), s.progress * 100, s.download_rate / 1000, s.upload_rate / 1000, \
                            s.num_peers, state_str[s.state]))
        except Exception as e:
            traceback.print_exc()

    def __call__(self, op, path, *args):
        cid = random.randint(10000, 20000)
        if self.LOGLEVEL > 4: print op, path, ('...data...' if op=='write' else args), cid
        if path.startswith('/.Trash'): raise FuseOSError(errno.EACCES)
        if path.endswith('/.__delugefs_dir__'): raise FuseOSError(errno.EACCES)
        ret = super(DelugeFS, self).__call__(op, path, *args)
        if self.LOGLEVEL > 4: print '...', cid
        return ret

    '''
    Fuse FS calls in alphabetical order
    '''
    def access(self, path, mode):
        fn = os.path.join(self.indexdir, path).encode(FS_ENCODE)
        if not os.access(fn, mode):
            raise FuseOSError(errno.EACCES)
        #return os.access(fn, mode)

    chmod = None

    chown = None

    def create(self, path, mode):
        with self.rwlock:
            tmp = uuid.uuid4().hex
            self.open_files[path] = tmp
            fn = os.path.join(self.indexdir, path).encode(FS_ENCODE)
            print 'readdir', fn
            print 'index', self.indexdir
            print 'path', path
            with open(fn,'wb') as f:
                pass
            return os.open(os.path.join(self.tmp, tmp).encode(FS_ENCODE), os.O_WRONLY | os.O_CREAT, mode)

    def flush(self, path, fh):
        with self.rwlock:
            return os.fsync(fh)

    def fsync(self, path, datasync, fh):
        with self.rwlock:
            return self.flush(path, fh)

    def getattr(self, path, fh=None):
        st_size = None
        if path in self.open_files:
            fn = os.path.join(self.tmp, self.open_files[path]).encode(FS_ENCODE)
        else:
            fn = os.path.join(self.indexdir, path).encode(FS_ENCODE)
            torrent = get_torrent_dict(fn)
            if torrent:
                torrent_info = torrent['info']  if torrent else None
                st_size = torrent_info['length'] if torrent_info else 0
        st = os.lstat(fn)
        ret = dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
            'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid'))
        if st_size is not None:
            ret['st_size'] = st_size
        if self.LOGLEVEL > 4: print ret
        return ret

    getxattr = None

    link = None

    listxattr = None

    def mkdir(self, path, flags):
        with self.rwlock:
            fn = os.path.join(self.indexdir, path).encode(FS_ENCODE)
            os.mkdir(fn, flags)
            with open(os.path.join(fn, '.__delugefs_dir__').encode(FS_ENCODE),'w') as f:
                f.write("git doesn't track empty dirs, so we add this file.")
            return 0

    mknod = None

    def open(self, path, flags):
        with self.rwlock:
            fn = os.path.join(self.indexdir, path).encode(FS_ENCODE)
            if not (flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_TRUNC)):
                if self.LOGLEVEL > 3: print '\treadonly'
                t = get_torrent_dict(fn)
                if t:
                    name = t['info']['name']
                    dat_fn = os.path.join(self.chunksdir, name[:2], name).encode(FS_ENCODE)
                    if not os.path.isfile(dat_fn):
                        self.__add_torrent(t, path)
                    self.last_read_file[path] = datetime.datetime.now()
                    return os.open(dat_fn, flags)
                else:
                    return os.open(fn, flags)
            else:
                # read and write below
                if path in self.open_files:
                    if self.LOGLEVEL > 3: print '%s in self.open_files' % (path)
                    tmp = self.open_files[path]
                else:
                    if self.LOGLEVEL > 3: print '%s NOT in self.open_files' % (path)
                    tmp = uuid.uuid4().hex
                    if os.path.isfile(fn):
                        with open(fn, 'rb') as f:
                            prev = get_torrent_dict['info']['name']
                            prev_fn = os.path.join(self.chunksdir, prev[:2], prev).encode(FS_ENCODE)
                            if os.path.isfile(prev_fn):
                                if self.LOGLEVEL > 3: print 'shutil.copy to tmp'
                                shutil.copyfile(prev_fn, os.path.join(self.tmp, tmp).encode(FS_ENCODE))
                    self.open_files[path] = tmp
                return os.open(os.path.join(self.tmp, tmp).encode(FS_ENCODE), flags)

    def read(self, path, size, offset, fh):
        with self.rwlock:
            if self.LOGLEVEL > 3: print 'path in self.bt_in_progress', path in self.bt_in_progress
            if path in self.bt_in_progress:
                if self.LOGLEVEL > 3: print '%s in progress' % (path)
                h = self.httpd.bt_handles[path]
                if not h.is_seed():
                    torrent_info = h.get_torrent_info()
                    piece_length = torrent_info.piece_length()
                    num_pieces = torrent_info.num_pieces()
                    start_index = offset // piece_length
                    end_index = (offset+size) // piece_length
                    if self.LOGLEVEL > 3: print 'pieces', start_index, end_index
                    priorities = h.piece_priorities()
                    #print 'priorities', priorities
                    for i in range(start_index, min(end_index+1,num_pieces)):
                        priorities[i] = 7
                    h.prioritize_pieces(priorities)
                    if self.LOGLEVEL > 3: print 'priorities', priorities
                    #  h.piece_priority(i, 8)
                    #print 'piece_priorities set'
                    for i in range(start_index, min(end_index+1,num_pieces)):
                        if self.LOGLEVEL > 3: print 'waiting for', i
                        for i in range(10):
                            if h.have_piece(i): break
                            time.sleep(1)
                        if self.LOGLEVEL > 3: print 'we have', i
            os.lseek(fh, offset, 0)
            ret = os.read(fh, size)
            #print 'ret', ret
            return ret

    def readdir(self, path, fh):
        with self.rwlock:
            fn = os.path.join(self.indexdir, path).encode(FS_ENCODE)
            return ['.', '..'] + [x for x in os.listdir(fn) if x!=".__delugefs_dir__" ]

    readlink = None

    def release(self, path, fh):
        with self.rwlock:
            ret = os.close(fh)
            if self.LOGLEVEL > 3: print 'ret', ret, path
            if path in self.open_files:
                self.__finalize(path, self.open_files[path])
                # moved to __finalize_callback
                # del self.open_files[path]
            if path in self.bt_in_progress:
                h = self.httpd.bt_handles[path]
                priorities = h.piece_priorities()
                #h.prioritize_pieces([0 for x in priorities])
            return 0

    def rename(self, old, new):
        with self.rwlock:
            fn = os.path.join(self.indexdir,new).encode(FS_ENCODE)
            if os.path.isfile(fn):
                os.remove(fn)
            os.rename(self.indexdir+old, self.indexdir+new)
            return 0

    def rmdir(self, path):
        with self.rwlock:
            fn = os.path.join(self.indexdir, path, '.__delugefs_dir__').encode(FS_ENCODE)
            if os.path.isfile(fn):
                os.remove(fn)
            dn = os.path.dirname(dn)
            if os.path.isdir(dn):
                os.rmdir(dn)
            return 0

    def statfs(self, path):
        fn = os.path.join(self.indexdir, path).encode(FS_ENCODE)
        if self.LOGLEVEL > 3: print 'statfs %s' % (path)
        stv = os.statvfs(fn)
        return dict((key, getattr(stv, key)) for key in ('f_bavail', 'f_bfree',
            'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
            'f_frsize', 'f_namemax'))

    symlink = None

    def truncate(self, path, length, fh=None):
        with self.rwlock:
            if self.LOGLEVEL > 3: print 'truncate fh is ', fh
            fn = os.path.join(self.indexdir, path).encode(FS_ENCODE)
            if path in self.open_files: # file was opened in READWRITE
                with open(os.path.join(self.tmp, self.open_files[path]).encode(FS_ENCODE), 'r+') as f:
                    f.truncate(length)
                return 0
            else: # file was opened in READONLY
                if self.LOGLEVEL > 3: print '%s not in open_files' % (path)
                # open from dat_fn
                t = get_torrent_dict(fn)
                if t:
                    name = t['info']['name']
                    if self.LOGLEVEL > 3: print '%s is %s' % (path, name)
                    dat_fn = os.path.join(self.chunksdir, name[:2], name).encode(FS_ENCODE)
                    if not os.path.isfile(dat_fn):
                        if self.LOGLEVEL > 3: print 'truncate __add_torrent'
                        self.__add_torrent(t, path)
                    self.last_read_file[path] = datetime.datetime.now()
                    with open(dat_fn, 'r+') as f:
                        if self.LOGLEVEL > 3: print 'truncate f.truncate'
                        f.truncate(length)
                    return 0

    utimens = None

    def unlink(self, path):
        with self.rwlock:
            fn = os.path.join(self.indexdir, path).encode(FS_ENCODE)
            torrent = get_torrent_dict(fn)
            torrent_info = torrent.get('info')  if torrent else None
            name = torrent_info.get('name') if torrent_info else ''
            dfn = os.path.join(self.chunksdir, name[:2], name).encode(FS_ENCODE)
            if os.path.isfile(dfn):
                os.remove(dfn)
                print 'deleted', dfn
            os.remove(fn)
            return 0

    def write(self, path, data, offset, fh):
        with self.rwlock:
            os.lseek(fh, offset, 0)
            return os.write(fh, data)


'''
Global functions
'''
'''
get_torrent_dict
----
opens a file and returns a bdecoded object
'''
def get_torrent_dict(fn):
    if not os.path.isfile(fn): return
    with open(fn, 'rb') as f:
        return libtorrent.bdecode(f.read())

'''
prune_empty_dirs
----
recursively remove empty directories
'''
def prune_empty_dirs(path):
    empty = True
    for fn in os.listdir(path):
        ffn = os.path.join(path, fn).encode(FS_ENCODE)
        if os.path.isdir(ffn):
            if not prune_empty_dirs(ffn):
                empty = False
        else:
            empty = False
    if empty:
        print 'pruning', path
        os.rmdir(path)
    return empty

'''
sha256sum
----
returns the sha256 hash of a file by reading blocks to reduce memory footprint
'''
def sha256sum(filename, blocksize=65536):
    hash = hashlib.sha256()
    with open(filename, "r+b") as f:
        for block in iter(lambda: f.read(blocksize), ""):
            hash.update(block)
    return hash.hexdigest()
