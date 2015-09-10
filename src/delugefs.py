#!/usr/bin/env python2.7

'''
DelugeFS - A shared-nothing distributed filesystem built using Python, Bittorrent, Git and Zeroconf
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
along with this program; if not, write to the Free Software
Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
'''

import os, errno, sys, threading, collections, uuid, shutil, traceback, random, select, time, socket, multiprocessing, stat, datetime, statvfs, math
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn
import libtorrent as lt
import sh
import pybonjour

SECONDS_TO_NEXT_CHECK = 120
FS_ENCODE = sys.getfilesystemencoding()
if not FS_ENCODE: FS_ENCODE = 'utf-8'

class Peer(object):
    def __init__(self, service_name, host):
        self.service_name = service_name
        self.host = host
        self.addr = socket.gethostbyname(host)
        self.bt_port = None
        self.ssh_port = None
        #TODO replace self.free_space = self.server.__get_free_space()

class DelugeFS(LoggingMixIn, Operations):
    def __init__(self, name, root, bt_start_port, sshport, loglevel, create=False):
        self.bootstrapping = True
        self.LOGLEVEL = loglevel
        self.name = name
        self.bj_name = self.name+'__'+uuid.uuid4().hex
        self.root = os.path.realpath(root)
        self.repodb = os.path.join(self.root, u'gitdb')
        self.tmp = os.path.join(self.root, 'tmp')
        self.dat = os.path.join(self.root, 'dat')
        self.ssh_port = sshport
        self.peers = {}
        self.bt_handles = {}
        self.bt_in_progress = set()
        self.should_push = False
        self.next_time_to_check_for_undermirrored_files = datetime.datetime.now() + datetime.timedelta(0,10+random.randint(0,30))
        self.last_read_file = {}

        if not os.path.isdir(self.root):
            os.mkdir(self.root)

        self.bt_session = lt.session()
        self.bt_session.listen_on(bt_start_port, bt_start_port+10)
        pe_settings = lt.pe_settings()
        pe_enc_policy = {0:lt.enc_policy.forced, 1:lt.enc_policy.enabled, 2:lt.enc_policy.disabled}
        pe_settings.out_enc_policy = lt.enc_policy(pe_enc_policy[0])
        pe_settings.in_enc_policy = lt.enc_policy(pe_enc_policy[0])
        pe_enc_level = {0:lt.enc_level.plaintext, 1:lt.enc_level.rc4, 2:lt.enc_level.both}
        pe_settings.allowed_enc_level = lt.enc_level(pe_enc_level[1])
        self.bt_session.set_pe_settings(pe_settings)
        self.bt_port = self.bt_session.listen_port()
        # self.bt_session.start_lsd() # no libtorrent local discovery because not sure if all local torrent clients are safe
        self.bt_session.start_dht()
        self.repo = None
        print 'libtorrent listening on:', self.bt_port
        self.bt_session.add_dht_router('localhost', 10670)
        print '...is_dht_running()', self.bt_session.dht_state()

        t = threading.Thread(target=self.__start_listening_bonjour_ssh)
        t.daemon = True
        t.start()
        t = threading.Thread(target=self.__start_listening_bonjour)
        t.daemon = True
        t.start()
        print 'give me a sec to look for other peers...'
        time.sleep(2)

        # create a symlink so we can git pull remotely from a standard location
        if not os.path.exists('/usr/home/delugefs/symlinks'):
            os.mkdir('/usr/home/delugefs/symlinks')
        if not os.path.exists('/usr/home/delugefs/symlinks/%s' % (self.name)):
            os.symlink(self.root, '/usr/home/delugefs/symlinks/%s' % (self.name))
        self.repo = sh.git.bake(_cwd=self.repodb)
        cnfn = os.path.join(self.repodb, '.__delugefs__', 'cluster_name')
        if create:
            if os.listdir(self.root):
                raise Exception('--create specified, but %s is not empty' % self.root)
            if self.peers:
                raise Exception('--create specified, but i found %i peer%s using --id "%s" already' % (len(self.peers), 's' if len(self.peers)>1 else '', self.name))
            if not os.path.isdir(self.repodb): os.mkdir(self.repodb)
            self.repo.init()
            os.mkdir(os.path.join(self.repodb, '.__delugefs__'))
            with open(cnfn, 'w') as f:
                f.write(self.name)
            self.repo.add(cnfn)
            self.repo.commit(m='repo created')
        else:
            if os.path.isfile(cnfn):
                with open(cnfn, 'r') as f:
                    existing_cluster_name = f.read().strip()
                    if existing_cluster_name != self.name:
                        raise Exception('a cluster root exists at %s, but its name is "%s", not "%s"' % (self.root, existing_cluster_name, self.name))
            else:
                if os.listdir(self.root):
                    raise Exception('root %s is not empty, but no cluster was found' % self.root)
                if not self.peers:
                    raise Exception('--create not specified, no repo exists at %s and no peers of cluster "%s" found' % (self.root, self.name))
                try:
                    apeer = self.peers[iter(self.peers).next()]
                    if not os.path.isdir(self.repodb): os.mkdir(self.repodb)
                    cloned = False;
                    while cloned == False:
                        if apeer.ssh_port is not None:
                            print 'clone ssh://%s:%i/usr/home/delugefs/symlinks/%s/gitdb' % (apeer.host, apeer.ssh_port, self.name)
                            self.repo.clone('ssh://%s:%i/usr/home/delugefs/symlinks/%s/gitdb' % (apeer.host, apeer.ssh_port, self.name), self.repodb)
                            cloned = True
                        time.sleep(1)
                    del cloned
                except Exception as e:
                    if os.path.isdir(os.path.join(self.repodb, '.git')):
                        shutil.rmtree(self.repodb)
                    traceback.print_exc()
                    raise e
                print 'success cloning repo!'

#        for path in self.repo.hg_status()['?']:
#            fn = os.path.join(self.repodb, path)
#            print 'deleting untracked file', fn
#            os.remove(fn)

        prune_empty_dirs(self.repodb)

        print '='*80

        if not os.path.isdir(self.tmp): os.makedirs(self.tmp)
        for fn in os.listdir(self.tmp): os.remove(os.path.join(self.tmp,fn))
        if not os.path.isdir(self.dat): os.makedirs(self.dat)
        self.rwlock = threading.Lock()
        self.open_files = {} # used to track opened files except READONLY
        print 'init', self.repodb
        self.repo.status()
        self.bootstrapping = False

        t = threading.Thread(target=self.__register_ssh, args=())
        t.daemon = True
        t.start()

        t = threading.Thread(target=self.__register, args=())
        t.daemon = True
        t.start()

        t = threading.Thread(target=self.__keep_pushing)
        t.daemon = True
        t.start()

        t = threading.Thread(target=self.__load_local_torrents)
        t.daemon = True
        t.start()

        t = threading.Thread(target=self.__monitor)
        t.daemon = True
        t.start()

    def __add_torrent(self, torrent, path):
        uid = torrent['info']['name']
        info = lt.torrent_info(torrent)
        dat_file = os.path.join(self.dat, uid[:2], uid)
        #print 'dat_file', dat_file
        if not os.path.isdir(os.path.dirname(dat_file)): os.mkdir(os.path.dirname(dat_file))
        h = self.bt_session.add_torrent({'ti':info, 'save_path':os.path.join(self.dat, uid[:2])})
        #h = self.bt_session.add_torrent(info, os.path.join(self.dat, uid[:2]), storage_mode=lt.storage_mode_t.storage_mode_sparse)
        if self.LOGLEVEL > 2: print 'added', uid
        self.bt_handles[path] = h

    def __add_torrent_and_wait(self, path, t):
        uid = t['info']['name']
        info = lt.torrent_info(t)
        dat_file = os.path.join(self.dat, uid[:2], uid)
        if not os.path.isdir(os.path.dirname(dat_file)): os.mkdir(os.path.dirname(dat_file))
        if not os.path.isfile(dat_file):
            with open(dat_file,'wb') as f:
                pass
        h = self.bt_session.add_torrent({'ti':info, 'save_path':os.path.join(self.dat, uid[:2])})
        #h.set_sequential_download(True)
        for peer in self.peers.values():
            if peer.bt_port is not None:
                print 'adding peer:', (peer.addr, peer.bt_port)
                h.connect_peer((peer.addr, peer.bt_port), 0)
        print 'added ', path
        self.bt_handles[path] = h
        self.bt_in_progress.add(path)
        #while not os.path.isfile(dat_file):
        #    time.sleep(.1)
        print 'file created'

    def __bonjour_browse_callback(self, sdRef, flags, interfaceIndex, errorCode, serviceName, regtype, replyDomain):
        #print 'browse_callback', sdRef, flags, interfaceIndex, errorCode, serviceName, regtype, replyDomain
        if errorCode != pybonjour.kDNSServiceErr_NoError:
            return
        if not (flags & pybonjour.kDNSServiceFlagsAdd):
            #print 'browse_callback service removed', sdRef, flags, interfaceIndex, errorCode, serviceName, regtype, replyDomain
            if serviceName in self.peers:
                del self.peers[serviceName]
            print 'self.peers', self.peers
            return
        #print 'Service added; resolving'
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

    def __bonjour_register_callback_ssh(self, sdRef, flags, errorCode, name, regtype, domain):
        if errorCode == pybonjour.kDNSServiceErr_NoError:
            print '...bonjour listener', name+'.'+regtype+domain, 'now listening on', self.ssh_port

    def __bonjour_resolve_callback(self, sdRef, flags, interfaceIndex, errorCode, fullname, hosttarget, port, txtRecord):
        if errorCode == pybonjour.kDNSServiceErr_NoError:
            if fullname.startswith(self.bj_name):
                #print 'ignoring my own service'
                return
            if not (fullname.startswith(self.name+'__') and ('._delugefs._tcp.' in fullname or '._ssh._tcp.' in fullname)):
                #print 'ignoring unrelated service', fullname
                return
            #print 'resolve_callback', sdRef, flags, interfaceIndex, errorCode, fullname, hosttarget, port, txtRecord
            sname = fullname[:fullname.index('.')]
            resolved.append(True)
            # strip '.local.' from host
            shost = hosttarget.split('.')[0]
            if not sname in self.peers:
                apeer = Peer(sname, shost)
            else:
                apeer = self.peers[sname]
            if '._ssh._tcp.' in fullname:
                apeer.ssh_port = port
            if '._delugefs._tcp.' in fullname:
                apeer.bt_port = port
            # save apeer to peers after modifying above
            self.peers[sname] = apeer
            if '._ssh._tcp.' in fullname:
                # only pull if finished bootstrapping
                if self.bootstrapping == False:
                    # only pull if delugefs detected
                    print 'self.peers', self.peers
                    if self.repo is not None:
                        if apeer.ssh_port is not None:
                            print 'self.repo.pull ssh://%s:%i/usr/home/delugefs/symlinks/%s/gitdb refs/heads/master:refs/remotes/%s/master' % (apeer.host, apeer.ssh_port, self.name, apeer.host)
                            self.repo.pull('ssh://%s:%i/usr/home/delugefs/symlinks/%s/gitdb' % (apeer.host, apeer.ssh_port, self.name),
                                        'refs/heads/master:refs/remotes/%s/master' % (apeer.host))
                            return 'pulling from new peer', apeer

    def __check_for_undermirrored_files(self):
        if self.next_time_to_check_for_undermirrored_files > datetime.datetime.now(): return
        try:
            print 'check_for_undermirrored_files @', datetime.datetime.now()
            my_uids = set(self.get_active_info_hashes())
            counter = collections.Counter(my_uids)
            peer_free_space = {'__self__': self.__get_free_space()}
            uid_peers = collections.defaultdict(set)
            for uid in my_uids:
                uid_peers[uid].add('__self__')
            for peer_id, peer in self.peers.items():
                for s in peer.server.get_active_info_hashes():
                    counter[s] += 1
                    uid_peers[s].add(peer_id)
                peer.free_space = peer.server.__get_free_space()
                peer_free_space[peer_id] = peer.free_space
            print 'counter', counter
            print 'peer_free_space', peer_free_space
            if len(peer_free_space) < 2:
                print "can't do anything, since i'm the only peer!"
                return
            fs_free_space = sum(peer_free_space.values()) / 2 / math.pow(2,30)
            print 'fs_free_space: %0.2fGB' % fs_free_space
            for root, dirs, files in os.walk(self.repodb):
                #print 'root, dirs, files', root, dirs, files
                if root.startswith(os.path.join(self.repodb, '.git')): continue
                if root.startswith(os.path.join(self.repodb, '.__delugefs__')): continue
                for fn in files:
                    if fn=='.__delugefs_dir__': continue
                    fn = os.path.join(root, fn)
                    e = get_torrent_dict(fn)
                    if not e:
                        print 'not a torrent?', fn
                        continue
                    uid = e['info']['name']
                    size = e['info']['length']
                    path = fn[len(self.repodb):]
                    if counter[uid] < 2:
                        peer_free_space_list = sorted([x for x in peer_free_space.items() if x[0] not in uid_peers[uid]], lambda x,y: x[1]<y[1])
                        print 'peer_free_space_list', peer_free_space_list
                        for best_peer_id, free_space in peer_free_space_list:
                            if uid in my_uids and best_peer_id=='__self__':
                                best_peer_id = peer_free_space_list[1][0]
                            peer_free_space[best_peer_id] -= size
                            print 'need to rep', path, 'to', best_peer_id
                            if '__self__'==best_peer_id:
                                self.__please_mirror(path)
                            else:
                                self.peers[best_peer_id].server.__please_mirror(path)
                                self.peers[best_peer_id].free_space -= size
                            break
                    if counter[uid] > 3:
                        print 'uid_peers', uid_peers
                        peer_free_space_list = sorted([x for x in peer_free_space.items() if x[0] in uid_peers[uid]], lambda x,y: x[1]>y[1])
                        print 'peer_free_space_list2', peer_free_space_list
                        for best_peer_id, free_space in peer_free_space_list:
                            if '__self__'==best_peer_id:
                                if self.__please_stop_mirroring(path): break
                            else:
                                if self.peers[best_peer_id].server.__please_stop_mirroring(path): break
                                self.peers[best_peer_id].free_space += size
                        print '__please_stop_mirroring', path, 'sent to', best_peer_id
        except Exception as e:
            traceback.print_exc()
        self.next_time_to_check_for_undermirrored_files = datetime.datetime.now() + datetime.timedelta(0,SECONDS_TO_NEXT_CHECK+random.randint(0,10*(1+len(self.peers))))

    def __get_free_space(self):
        f = os.statvfs(self.root)
        return f[statvfs.F_BSIZE] * f[statvfs.F_BFREE]

    def __keep_pushing(self):
        self.pushed_to = {}
        while True:
            if self.should_push:
                # set default to not pushed if not exist
                for peer in self.peers.values():
                    if not peer in self.pushed_to:
                        self.pushed_to[peer] = False
                # do the push
                for peer in self.peers.values():
                    if self.pushed_to[peer] == False:
                        if self.repo is not None:
                            if peer.ssh_port is not None:
                                print 'self.repo.push ssh://%s:%i/usr/home/delugefs/symlinks/%s/gitdb master:refs/heads/tomerge' % (peer.host, peer.ssh_port, self.name)
                                self.repo.push('ssh://%s:%i/usr/home/delugefs/symlinks/%s/gitdb' % (peer.host, peer.ssh_port, self.name),
                                                'master:refs/heads/tomerge')
                                self.pushed_to[peer] = True
                # set to false...
                self.should_push = False
                # but if any are true, set to keep_pushing
                for peer in self.peers.values():
                    if self.pushed_to[peer] == False:
                        self.should_push = True
                # if all set to false, reset pushed_to
                if self.should_push == False:
                    for peer in self.peers.values():
                        self.pushed_to[peer] = False
            time.sleep(1)

    def __load_local_torrents(self):
        #print 'self.repodb', self.repodb
        for root, dirs, files in os.walk(self.repodb):
            #print 'root, dirs, files', root, dirs, files
            if root.startswith(os.path.join(self.repodb, '.git')): continue
            if root.startswith(os.path.join(self.repodb, '.__delugefs__')): continue
            for fn in files:
                if fn=='.__delugefs_dir__': continue
                fn = os.path.join(root, fn)
                print 'loading torrent', fn
                e = get_torrent_dict(fn)
                if not e:
                    print 'not able to read torrent', fn
                    continue
                #with open(fn,'rb') as f:
                #  e = lt.bdecode(f.read())
                uid = e['info']['name']
                info = lt.torrent_info(e)
                dat_file = os.path.join(self.dat, uid[:2], uid)
                #print 'dat_file', dat_file
                if os.path.isfile(dat_file):
                    if not os.path.isdir(os.path.dirname(dat_file)): os.mkdir(os.path.dirname(dat_file))
                    h = self.bt_session.add_torrent({'ti':info, 'save_path':os.path.join(self.dat, uid[:2])})
                    #h = self.bt_session.add_torrent(info, os.path.join(self.dat, uid[:2]), storage_mode=lt.storage_mode_t.storage_mode_sparse)
                    print 'added ', fn, '(%s)'%uid
                    self.bt_handles[fn[len(self.repodb):]] = h
        print 'self.bt_handles', self.bt_handles

    def __monitor(self):
        while True:
            time.sleep(random.randint(3,7))
            #print '='*80
            self.__write_active_torrents()
            #TODO replace self.__check_for_undermirrored_files()

    def __please_mirror(self, path):
        try:
            print '__please_mirror', path
            fn = self.repodb+path
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

            h = self.bt_handles[path]
            if h:
                uid = h.get_torrent_info().name()
                self.bt_session.remove_torrent(h)
                fn = os.path.join(self.dat, uid[:2], uid)
                if os.path.isfile(fn): os.remove(fn)
                print 'stopped mirroring', path
                return True
            return False
        except:
            traceback.print_exc()
            return False

    def __register(self):
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

    def __register_ssh(self):
        print 'registering bonjour listener for ssh...'
        bjservice = pybonjour.DNSServiceRegister(name=self.bj_name, regtype="_ssh._tcp",
                        port=self.ssh_port, callBack=self.__bonjour_register_callback_ssh)
        try:
            while True:
                ready = select.select([bjservice], [], [])
                if bjservice in ready[0]:
                    pybonjour.DNSServiceProcessResult(bjservice)
        except KeyboardInterrupt:
            pass

    def __start_listening_bonjour(self):
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

    def __start_listening_bonjour_ssh(self):
        browse_sdRef = pybonjour.DNSServiceBrowse(regtype="_ssh._tcp", callBack=self.__bonjour_browse_callback)
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

    def __write_active_torrents(self):
        try:
            with open(os.path.join(self.repodb, '.__delugefs__', 'active_torrents'), 'w') as f:
                for path, h in self.bt_handles.items():
                    s = h.status()
#                    torrent_peers = h.get_peer_info()
#                    print 'torrent_peers', torrent_peers
#                    if len(torrent_peers) < 1:
#                        print 'only', len(torrent_peers), 'peer for', path
#                        if self.peers:
#                            peer = self.peers.values()[random.randint(0,len(self.peers)-1)]
#                            peer.server.__please_mirror(path)
                    #if s.state==5 and s.download_rate==0 and s.upload_rate==0: continue
                    state_str = ['queued', 'checking', 'downloading metadata', 'downloading', 'finished', 'seeding', 'allocating', 'checking resume data']
                    f.write('%s %s is %.2f%% complete (down: %.1f kb/s up: %.1f kB/s peers: %d) %s\n' % \
                            (path, h.get_torrent_info().name(), s.progress * 100, s.download_rate / 1000, s.upload_rate / 1000, \
                            s.num_peers, state_str[s.state]))
        except Exception as e:
            traceback.print_exc()


    def get_active_info_hashes(self):
        self.next_time_to_check_for_undermirrored_files = datetime.datetime.now() + datetime.timedelta(0,SECONDS_TO_NEXT_CHECK+random.randint(0,10*(1+len(self.peers))))
        active_info_hashes = []
        for k,h in self.bt_handles.items():
            if not h: continue
            try:
                active_info_hashes.append(str(h.get_torrent_info().name()))
            except:
                traceback.print_exc()
                del self.bt_handles[k]
        print 'active_info_hashes', active_info_hashes
        return active_info_hashes

    def __call__(self, op, path, *args):
        cid = random.randint(10000, 20000)
        if self.LOGLEVEL > 4: print op, path, ('...data...' if op=='write' else args), cid
        if path.startswith('/.Trash'): raise FuseOSError(errno.EACCES)
        if path.endswith('/.__delugefs_dir__'): raise FuseOSError(errno.EACCES)
        ret = super(DelugeFS, self).__call__(op, path, *args)
        if self.LOGLEVEL > 4: print '...', cid
        return ret

    def access(self, path, mode):
        if not os.access(self.repodb+path, mode):
            raise FuseOSError(errno.EACCES)
        #return os.access(self.repodb+path, mode)

#    chmod = os.chmod
#    chown = os.chown

    def create(self, path, mode):
        with self.rwlock:
            if path.startswith('/.__delugefs__'): return 0
            tmp = uuid.uuid4().hex
            self.open_files[path] = tmp
            with open(self.repodb+path,'wb') as f:
                pass
            #self.repo.add(self.repodb+path) # blank file, no point in adding to repo
            return os.open(os.path.join(self.tmp, tmp), os.O_WRONLY | os.O_CREAT, mode)

    def flush(self, path, fh):
        with self.rwlock:
            return os.fsync(fh)

    def fsync(self, path, datasync, fh):
        with self.rwlock:
            return os.fsync(fh)

    def getattr(self, path, fh=None):
        st_size = None
        if path in self.open_files:
            fn = os.path.join(self.tmp, self.open_files[path])
        else:
            fn = self.repodb+path
            if os.path.isfile(fn):
                if not path.startswith('/.__delugefs__'):
                    with open(fn, 'rb') as f:
                        torrent = lt.bdecode(f.read())
                        torrent_info = torrent.get('info')  if torrent else None
                        st_size = torrent_info.get('length') if torrent_info else 0
        st = os.lstat(fn)
        ret = dict((key, getattr(st, key)) for key in ('st_atime', 'st_ctime',
            'st_gid', 'st_mode', 'st_mtime', 'st_nlink', 'st_size', 'st_uid'))
        if path.startswith('/.__delugefs__'):
            ret['st_mode'] = ret['st_mode'] & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) # no write for group/other
        if st_size is not None:
            ret['st_size'] = st_size
        if self.LOGLEVEL > 4: print ret
        return ret

    getxattr = None

# TODO XXX FIX
#    def link(self, target, source):
#        with self.rwlock:
#            return os.link(source, target)

    listxattr = None
#    mknod = os.mknod

    def mkdir(self, path, flags):
        with self.rwlock:
            if path.startswith('/.__delugefs__'): return 0
            fn = self.repodb+path
            ret = os.mkdir(fn, flags)
            with open(fn+'/.__delugefs_dir__','w') as f:
                f.write("git doesn't track empty dirs, so we add this file...")
            self.repo.add(fn+'/.__delugefs_dir__')
            self.repo.commit(m='mkdir '+path)
            self.should_push = True
            return ret

    def read(self, path, size, offset, fh):
        with self.rwlock:
            if self.LOGLEVEL > 3: print 'path in self.bt_in_progress', path in self.bt_in_progress
            if path in self.bt_in_progress:
                if self.LOGLEVEL > 3: print '%s in progress' % (path)
                h = self.bt_handles[path]
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

    def open(self, path, flags):
        with self.rwlock:
            fn = self.repodb+path
            if not (flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_TRUNC)):
                if path.startswith('/.__delugefs__'):
                    if self.LOGLEVEL > 3: print '='*80
                    return os.open(fn, flags)
                if self.LOGLEVEL > 3: print '\treadonly'
                t = get_torrent_dict(fn)
                if t:
                    name = t['info']['name']
                    dat_fn = os.path.join(self.dat, name[:2], name)
                    if not os.path.isfile(dat_fn):
                        self.__add_torrent_and_wait(path, t)
                    self.last_read_file[path] = datetime.datetime.now()
                    return os.open(dat_fn, flags)
                else:
                    return os.open(fn, flags)
            # read and write below
            if path.startswith('/.__delugefs__'): return 0
            if path in self.open_files:
                tmp = self.open_files[path]
            else:
                tmp = uuid.uuid4().hex
                if os.path.isfile(fn):
                    with open(fn, 'rb') as f:
                        prev = lt.bdecode(f.read())['info']['name']
                        prev_fn = os.path.join(self.dat, prev[:2], prev)
                        if os.path.isfile(prev_fn):
                            shutil.copyfile(prev_fn, os.path.join(self.tmp, tmp))
                self.open_files[path] = tmp
            return os.open(os.path.join(self.tmp, tmp), flags)


    def readdir(self, path, fh):
        with self.rwlock:
            return ['.', '..'] + [x for x in os.listdir(self.repodb+path) if x!=".__delugefs_dir__" and x!='.git']

#    readlink = os.readlink

    def release(self, path, fh):
        with self.rwlock:
            ret = os.close(fh)
            if self.LOGLEVEL > 3: print 'ret', ret, path
            if path in self.open_files:
                self.finalize(path, self.open_files[path])
                del self.open_files[path]
            if path in self.bt_in_progress:
                h = self.bt_handles[path]
                priorities = h.piece_priorities()
                #h.prioritize_pieces([0 for x in priorities])
            return ret

    def finalize(self, path, uid):
        if self.LOGLEVEL > 2: print 'finalize', path, uid
        try:
            fs = lt.file_storage()
            tmp_fn = os.path.join(self.tmp, uid)

            #print tmp_fn
            lt.add_files(fs, tmp_fn)
            t = lt.create_torrent(fs)
            t.set_creator("DelugeFS");
            lt.set_piece_hashes(t, self.tmp)
            tdata = t.generate()
            #print tdata
            with open(self.repodb+path, 'wb') as f:
                f.write(lt.bencode(tdata))
            self.repo.add(self.repodb+path)
            #print 'wrote', self.repodb+path
            dat_dir = os.path.join(self.dat, uid[:2])
            if not os.path.isdir(dat_dir):
                try: os.mkdir(dat_dir)
                except: pass
            os.rename(tmp_fn, os.path.join(dat_dir, uid))
            #print 'committing', self.repodb+path
            self.repo.commit(m='wrote '+path)
            self.should_push = True
            self.__add_torrent(tdata, path)
        except Exception as e:
            traceback.print_exc()
            raise e

    def rename(self, old, new):
        with self.rwlock:
            if old.startswith('/.__delugefs__'): return 0
            if new.startswith('/.__delugefs__'): return 0
            self.repo.mv(self.repodb+old, self.repodb+new)
            self.repo.commit(m='rename '+old+' to '+new)
            self.should_push = True

    def rmdir(self, path):
        with self.rwlock:
            if path.startswith('/.__delugefs__'): return 0
            fn = self.repodb+path+'/.__delugefs_dir__'
            if os.path.isfile(fn):
                self.repo.rm(fn)
                self.repo.commit(m='rmdir '+path)
                self.should_push = True
            if os.path.isdir(self.repodb+path):
                os.rmdir(self.repodb+path)

    def statfs(self, path):
        fn = self.repodb + path
        #print repr(fn)
        stv = os.statvfs(fn.encode(FS_ENCODE))
        return dict((key, getattr(stv, key)) for key in ('f_bavail', 'f_bfree',
            'f_blocks', 'f_bsize', 'f_favail', 'f_ffree', 'f_files', 'f_flag',
            'f_frsize', 'f_namemax'))

    def symlink(self, target, source):
        with self.rwlock:
            if target.startswith('/.__delugefs__'): return 0
            if src.startswith('/.__delugefs__'): return 0
            ret = os.symlink(source, target)
            return ret

    def truncate(self, path, length, fh=None):
        with self.rwlock:
            if self.LOGLEVEL > 3: print fh
            fn = self.repodb+path
            if path.startswith('/.__delugefs__'): return 0
            if path in self.open_files: # file was opened in READWRITE
                with open(os.path.join(self.tmp, self.open_files[path]), 'r+') as f:
                    f.truncate(length)
                return 0
            else: # file was opened in READONLY
                if self.LOGLEVEL > 3: print '%s not in open_files' % (path)
                # open from dat_fn
                t = get_torrent_dict(fn)
                if t:
                    name = t['info']['name']
                    if self.LOGLEVEL > 3: print '%s is %s' % (path, name)
                    dat_fn = os.path.join(self.dat, name[:2], name)
                    if not os.path.isfile(dat_fn):
                        if self.LOGLEVEL > 3: print 'truncate __add_torrent_and_wait'
                        self.__add_torrent_and_wait(path, t)
                    self.last_read_file[path] = datetime.datetime.now()
                    with open(dat_fn, 'r+') as f:
                        if self.LOGLEVEL > 3: print 'truncate f.truncate'
                        f.truncate(length)
                    return 0

#    utimens = os.utime

    def unlink(self, path):
        with self.rwlock:
            if path.startswith('/.__delugefs__'): return 0
            fn = (self.repodb+path).encode(FS_ENCODE)
            with open(fn, 'rb') as f:
                torrent = lt.bdecode(f.read())
                torrent_info = torrent.get('info')  if torrent else None
                name = torrent_info.get('name') if torrent_info else ''
                dfn = os.path.join(self.dat, name[:2], name)
                if os.path.isfile(dfn):
                    os.remove(dfn)
                    print 'deleted', dfn
            if True:#try:
                self.repo.rm(fn)
                self.repo.commit(m='unlink '+path)
                self.should_push = True
            #except Exception as e:
            #    if 'file is untracked' in str(e):
            #        os.remove(self.repodb+path)
            #    else:
            #        raise e

    def write(self, path, data, offset, fh):
        with self.rwlock:
            os.lseek(fh, offset, 0)
            return os.write(fh, data)




resolved = [] # for pybonjour callbacks




def get_torrent_dict(fn):
    if not os.path.isfile(fn): return
    with open(fn, 'rb') as f:
        return lt.bdecode(f.read())

def prune_empty_dirs(path):
    empty = True
    for fn in os.listdir(path):
        ffn = os.path.join(path, fn)
        if os.path.isdir(ffn):
            if not prune_empty_dirs(ffn):
                empty = False
        else:
            empty = False
    if empty:
        print 'pruning', path
        os.rmdir(path)
    return empty

def usage(msg):
    print 'ERROR:', msg
    print('usage: %s [--create] --cluster <clustername> --root <root> [--mount <mountpoint>]' % sys.argv[0])
    print '  cluster: any string uniquely identifying the desired cluster'
    print '  root: path to backend storage to use'
    print '  mount: path to FUSE mount location'
    sys.exit(1)




if __name__ == '__main__':
    config = {}
    k = None
    for s in sys.argv:
        if s.startswith('--'):
            if k:  config[k] = True
            k = s[2:]
        else:
            if k:
                config[k] = s.encode(FS_ENCODE)
                k = None
    if not 'cluster' in config:
        usage('cluster name not set')
    if not 'root' in config:
        usage('root not set')
    if not 'sshport' in config:
        sshport = 22
    else:
        sshport = int(config['sshport'])
    if not 'btport' in config:
        btport = random.randint(10000, 20000)
    else:
        btport = int(config['btport'])
    if not 'loglevel' in config:
        loglevel = 0
    else:
        loglevel = int(config['loglevel'])
    server = DelugeFS(config['cluster'], config['root'], btport, sshport, loglevel, create=config.get('create'))
    if 'mount' in config:
        if not os.path.exists(config['mount']):
            os.mkdir(config['mount'])
        fuse = FUSE(server, config['mount'], foreground=True, debug=True) #, direct_io=True) #, allow_other=True)
    else:
        while True:
            time.sleep(60)
