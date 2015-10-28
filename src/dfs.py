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

import os
import random
import sys
import time
import uuid
from fuse import FUSE, FuseOSError, Operations, LoggingMixIn
import libtorrent
import pybonjour
from delugefs import DelugeFS

FS_ENCODE = sys.getfilesystemencoding()
if not FS_ENCODE: FS_ENCODE = 'utf-8'
homepath = os.path.expanduser("~")

'''
usage
----
helper for command line usage
'''
def usage(msg):
    print 'ERROR:', msg
    print('usage: %s [--create] --cluster <clustername> --root <root> [--mount <mountpoint>]' % sys.argv[0])
    print '  cluster: any string uniquely identifying the desired cluster'
    print '  root: path to backend storage to use'
    print '  mount: path to FUSE mount location'
    sys.exit(1)

'''
main area called if command line
'''
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
    # one last time if k to catch ending --create or --lazy
    if k:  config[k] = True
    # print config

    if not 'cluster' in config:
        usage('cluster name not set')
    if not 'root' in config:
        usage('root not set')

    if not 'datacenter' in config:
        datacenter = uuid.uuid4().hex
    else:
        datacenter = config['datacenter']

    if 'webip' in config:
        webip = config['webip']
    else:
        webip = 'localhost'
    if 'webport' in config:
        webport = int(config['webport'])
    else:
        webport = None
    if 'webdir' in config:
        webdir = config['webdir']
    else:
        webdir = os.path.join(homepath, u'webui')

    if 'btport' in config:
        btport = int(config['btport'])
    else:
        btport = 4433 # SSL torrent or random.randint(60000, 61000)

    if 'loglevel' in config:
        loglevel = int(config['loglevel'])
    else:
        loglevel = 0

    if 'lazy' in config:
        lazy = True
    else:
        lazy = False

    delugefs = DelugeFS(config['cluster'], config['root'], config['datacenter'], btport, webip, webport, webdir, loglevel, lazy, create=config.get('create'))

    try:
        if 'mount' in config:
            delugefs.httpd.api['mount'] = config['mount']
            if not os.path.exists(config['mount']):
                os.mkdir(config['mount'])
            fuse = FUSE(delugefs, config['mount'], foreground=True) #,nothreads=True #, debug=True) #, allow_other=True)
        else:
            while True:
                time.sleep(60)
    finally:
        print 'attempting webui shutdown...'
        try:
            delugefs.httpd.shutdown()
        except:
            # it wasn't started
            pass
