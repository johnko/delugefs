#!/usr/bin/env python
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
along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

import BaseHTTPServer
import os
import SocketServer
import statvfs

'''
WebUIServer
----
This class will serve the webui
'''
class WebUIServer(SocketServer.ThreadingMixIn, BaseHTTPServer.HTTPServer):
    pass

'''
WebUIHandler
----
This class will handle the HTTP requests
'''
class WebUIHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    '''
    silence the logs
    '''
    def log_message(self, format, *args):
        return
    '''
    do_GET
    ----
    This function is run for every GET HTTP request
    '''
    def do_GET(self):
        try:
            var_req = "/api/v2/json/"
            file_req = "/api/v2/file/"
            if self.path[0:len(var_req)]==var_req:
                '''
                if we are parsing a api json request
                '''
                key = self.path[len(var_req):]
                if key in self.server.api:
                    self.header_plaintext()
                    self.wfile.write('[{"res":')
                    self.json_key(key)
                    self.wfile.write('}]')
                elif key == 'activetorrents':
                    '''
                    or key is activetorrents then we have to generate the output
                    '''
                    self.header_plaintext()
                    self.wfile.write('[{"res":')
                    self.json_torrents()
                    self.wfile.write(',')
                    '''
                    activetorrents secondary response of torrent peers
                    '''
                    self.wfile.write('"res2":')
                    self.json_btpeers()
                    self.wfile.write('}]')
                elif key == 'freespace':
                    '''
                    or key is freespace then we have to generate the output
                    '''
                    self.header_plaintext()
                    self.wfile.write('[{"res":')
                    self.json_freespace()
                    self.wfile.write('}]')
                elif key == 'peers':
                    '''
                    or key is peers then we have to generate the output
                    '''
                    self.header_plaintext()
                    self.wfile.write('[{"res":')
                    self.json_peers()
                    self.wfile.write('}]')
                elif key == 'all':
                    self.header_plaintext()
                    self.json_all()
                else:
                    '''
                    otherwise key is unknown
                    '''
                    self.send_error(404,'Data Not Found: %s' % self.path[len(var_req):])
            elif self.path[0:len(file_req)]==file_req:
                '''
                # TODO fix for reading chunks
                # if we are parsing a api file request (to download the file from webui)
                # Decode the .torrent file and serve the DAT if exists
                # print 'self.server.api[mount]', self.server.api['mount']
                file_path = self.path[len(file_req):]
                try:
                    pathparts = file_path.strip('/').split('/')
                    newpath = os.sep.join(pathparts)
                    # print 'pathparts',pathparts
                    # print 'newpath',newpath
                    repodb = os.path.join(self.server.api['root'], u'gitdb')
                    dat = os.path.join(self.server.api['root'], u'dat')
                    fn = os.path.join(repodb, newpath).encode(FS_ENCODE)
                    # parse the .torrent and read it straight from the dat folder
                    t = get_torrent_dict(fn)
                    if t:
                        name = t['info']['name']
                        dat_fn = os.path.join(dat, name[:2], name)
                        f = open(dat_fn)
                        self.send_response(200)
                        self.send_header('Content-type','application/octet-stream')
                        self.end_headers()
                        self.wfile.write(f.read())
                        f.close()
                    else:
                        self.send_error(404,'File Not Found: %s' % self.path)
                except IOError:
                    self.send_error(404,'File Not Found: %s' % self.path)
                '''
            else:
                '''
                else we are serving the home page, not parsing an api request
                '''
                self.webui()
        finally:
            pass

    def header_plaintext(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        return

    def json_all(self):
        self.wfile.write('{')
        for k in self.server.api:
            self.wfile.write('"%s":' % k)
            self.json_key(k)
            self.wfile.write(',')
        self.wfile.write('"btpeers":')
        self.json_btpeers()
        self.wfile.write(',')
        self.wfile.write('"peers":')
        self.json_peers()
        self.wfile.write(',')
        self.wfile.write('"activetorrents":')
        self.json_torrents()
        self.wfile.write(',')
        self.wfile.write('"freespace":')
        self.json_freespace()
        self.wfile.write('}')

    def json_btpeers(self):
        try:
            self.peerip
        except:
            self.peerip = set()
        self.wfile.write('[')
        count = 0
        for i in self.peerip:
            if count > 0: self.wfile.write(',')
            self.wfile.write('{"ip":"%s","bt_port":"%s"}' % (i.split(':')[0],i.split(':')[1]))
            if count < 1: count += 1
        self.wfile.write(']')

    def json_freespace(self):
        f = os.statvfs(self.server.api['root'])
        bsize = f[statvfs.F_BSIZE]
        if bsize > 4096: bsize = 512
        freebytes = (bsize * f[statvfs.F_BFREE]) / 1024 / 1024 / 1024
        self.wfile.write('"%d GB"' % freebytes)

    def json_key(self, key):
        if key in self.server.api:
            s = self.server.api[key]
            self.wfile.write('"%s"' % s)

    def json_peers(self):
        self.wfile.write('[')
        count = 0
        for s in self.server.peers.values():
            if count > 0: self.wfile.write(',')
            addr = None
            if s.addr is not None: addr = s.addr
            if (addr is None) and (self.server.nametoaddr[s.host] is not None): addr = self.server.nametoaddr[s.host]
            self.wfile.write('{"service_name":"%s","host":"%s","addr":"%s","bt_port":"%s"}' % (s.service_name, s.host, addr, str(s.bt_port)))
            if count < 1: count += 1
        self.wfile.write(']')

    def json_torrents(self):
        self.wfile.write('[')
        count = 0
        self.peerip = set()
        for path, h in self.server.bt_handles.items():
            if count > 0: self.wfile.write(',')
            urlpath = "/".join(path.split(os.sep))
            s = h.status()
            state_str = ['queued', 'checking', 'downloading metadata', 'downloading', 'finished', 'seeding', 'allocating', 'checking resume data']
            self.wfile.write('{"path":"%s","hash_name":"%s","progress":"%d","down":"%d","up":"%d","peers":"%d","state":"%s"}' % \
                    (urlpath, h.get_torrent_info().name(), s.progress * 100, s.download_rate, s.upload_rate, \
                    s.num_peers, state_str[s.state]))
            if count < 1: count += 1
            p = h.get_peer_info()
            for i in p:
                ip = '%s:%d' % (i.ip[0],i.ip[1]) # ip and port
                if ip not in self.peerip:
                    self.peerip.add(ip)
        self.wfile.write(']')

    def webui(self):
        if '/'==self.path: self.path = '/index.html'
        try:
            pathparts = self.path.split('/')
            newpath = os.sep.join(pathparts)
            f = open(os.path.join(self.server.api['webdir'], newpath[1:]))
            self.send_response(200)
            if self.path.endswith('.html'):
                self.send_header('Content-type','text/html')
            elif self.path.endswith('.js'):
                self.send_header('Content-type','text/javascript')
            self.end_headers()
            self.wfile.write(f.read())
            f.close()
            return
        except IOError:
            self.send_error(404,'File Not Found: %s' % self.path)
