This fork is brought to a simmer over medium-low heat, and modified to taste for FreeBSD.

# Overview

DelugeFS is a distributed filesystem [@keredson](https://github.com/keredson) implemented as a proof of concept of several ideas he's had over the past few years.

Key features include:

- a [shared-nothing architecture](http://en.wikipedia.org/wiki/Shared_nothing_architecture) (meaning there is no master node controlling everything - all peers are equal and there is no single point of failure)
- auto-discovery to suggest local network peers
- able to utilize highly heterogeneous computational resources (ie: a random bunch of disks stuck in a random number of machines)

Key insights this FS proves:

- Efficient distribution of large blocks of immutable data without centralized control is a solved problem. [libtorrent](http://www.rasterbar.com/products/libtorrent/).
- Efficient sharing of small quantities of mutable data (without a central server) is a solved problem. [Mercurial](http://mercurial.selenic.com/), or [Git](http://git-scm.com/), and [syncthing](https://syncthing.net/).
- Automatic discovery of peers on a local network is a solved problem. [Zeroconf/Bonjour](http://en.wikipedia.org/wiki/Zeroconf).
- Emulating a filesystem is a solved problem with [FUSE](http://en.wikipedia.org/wiki/Filesystem_in_Userspace).
- All of these projects have Python bindings!
- The key node in a distributed filesystem is the _disk_, not the machine. Everything above the disk is network topology.

![](https://github.com/johnko/delugefs/raw/master/screenshot.png)

# This Fork's Planned Changes

- try using [pyfilesystem](https://github.com/PyFilesystem/pyfilesystem)

- split files into chunks similar to [GridFS](https://docs.mongodb.org/manual/core/gridfs/)
- what size should the file chunks be? investigate how torrent chunks files
- store user desired filename as array of torrent hashes, save torrents in metadata folder, e.g. ./meta/torrents
- track chunks we NEW -ly added using a metadata folder/file (so other nodes can find us if MetaSyncLayer and SyncLayer are different)
- track chunks others HAVE using a metadata folder/file (so we can determine if we need to make another copy in some sort of reed-solomon algorithm)
- track chunks we WANT using a metadata folder/file (because all nodes can't seed forever, we use this to dynamically add/remove seeding requests for chunks)
- on read: verify the SHA256 before returning to FS

- split up the mount logic and the sync logic so that root can mount and talk to a an unprivileged MetaSyncLayer and SyncLayer
- MetaSyncLayer could use [syncthing](https://syncthing.net/) (with recursive folder scan disabled) for metadata and chunks in separate repos,
- or MetaSyncLayer syncthing for metadata and SyncLayer torrent for chunks (to grab from multiple sources)
- can we run multiple instances of syncthing on one computer with different programroot folders? or do we have to do something like OpenZFS vdevs?

- manually add external network peer (this should be handled by syncthing)
- Firewall hole punching for git on peers behind NAT/firewalls (handled by syncthing and torrent dht)
- remote the "keep_pushing" loop and let syncthing handle that
- remove git (let syncthing handle conflicts)
- local network peers autodiscovery to suggest in syncthing??

- if I/O across the FUSE boundary is still CPU limited around ~10MB/s, see if tmpfs/ramdisk will help

# This Fork's Planned Underlying Tree Layout

```
/mnt/disk1
    +- meta/                                    # was "gitdb", now MetaSyncLayer will handle syncing this across nodes
    |   +- new/                                 # on a FS write, each node writes what chunk it has added here
    |   |   `- SHA256.torrent                   # newly added chunks, if not lazy, auto-add these torrents
    |   |
    |   +- catalog/                             # on 100% dl, append the chunk SHA to your catalog
    |   |   `- node-programroot-safefoldername  # use programroot folder name in case we want...
    |   |                                       # another copy on a different folder without mounting
    |   +- want/
    |   |   `- SHA256.torrent                   # symlink to or copy from chunks we want that seeders...
    |   |                                       # should auto-add if they have it
    |   |
    |   +- torrents/                            # master library of chunk metadata
    |   |   +- (SHA256 depth+width)             # split like git, to avoid limit of approx 32000 files in a folder
    |   |       `- SHA256.torrent
    |   +- index/
    |       `- filename                         # traversing the FS, really just traverses this
    |
    +- chunks/                      # was "dat", used by FS and SyncLayer
    |   +- (SHA256 depth+width)     # split like git, to avoid limit of approx 32000 files in a folder
    |       `- SHA256               # the chunks, on read: verify the SHA256 before returning to FS
    |
    +- tmp/                 # stores data that is going to be written
        +- uuid.whole       # this is a whole file that needs to be chunked
        `- uuid.chunk       # this is a chunk of a file that we will SHA256, move to ./chunks/, then create a torrent
```

# This Fork's Implemented Changes

Tick if manually tested:

- [x] specify BitTorrent start port number with --port
- [x] Git instead of Mercurial
- [ ] SSH prep-node script to bootstrap id_ed25519.pub and known_hosts
- [ ] force SSH command in authorized_keys2 after "git push", "git merge tomerge" ... command="git-shell -c $SSH_ORIGINAL_COMMAND"
- [ ] torrent.info.name should be sha256 of file instead of uuid.hex so we can dedup if this creates similar torrent.info.hash

# TODO, but no idea yet on how to implement

- Find a better way to invalidate cache (instead of always invalidating it via sysctl)

# Requirements

FreeBSD:

- kldload fuse
- pkg install py27-pybonjour mDNSResponder
- pkg install py27-libtorrent-rasterbar libtorrent-rasterbar
- pkg install fusefs-libs
- pkg install syncthing
- pkg install git py27-sh # we want to get rid of this dependency by 0.4

# Current Status

**HIGHLY EXPERIMENTAL! -- PROOF OF CONCEPT ONLY -- DO NOT USE FOR ANY CRITICAL DATA AT THIS POINT!**

In 2013 [@keredson](https://github.com/keredson) was using it as personal media center storage spanning three disks on two machines. It works well so far, but it still very early in development.

Speed:

- I/O across the FUSE boundary is CPU limited. Max observed is ~10MB/s. [@keredson](https://github.com/keredson) suspects this is a limitation of the Python FUSE bindings.
- I/O between nodes is limited by the disk read/write speeds. [@keredson](https://github.com/keredson) has observed >70MB/s sustained on his home network.

## Known Issues

- Files over ~4GB are not stored (and their zero-length stubs cannot be deleted). [@keredson](https://github.com/keredson) believes this is due to an int vs. long incompatibility with libtorrent, but hasn't confirmed.
- hardlink and symlink fails
- set ctime/mtime on a file fails (may be fixed with syncthing)
- set owner fails, but doesn't error
- set permission fails, but doesn't error

# Basic Algorithm

To start up:

1. Filesystem is started given a volume id, a storage location, and a mount point.
2. Filesystem searches for local peers.
3. Filesystem either pulls from our clones other peer's repositories.
4. Filesystem looks for any files it has locally (complete or not), and starts seeding them.

To write a file:

1. Filesystem client opens a file and attempts to write. Filesystem returns a handle to a local temporary file.
2. Client writes to file and then closes it.
3. Filesystem splits file into chunks, store filename as array of chunks and size, chunks are named as secure hashes of its contents, create torrent of each chunk (containing metadata about the file along with secure hashes of its contents) and commits it to a local repository.
4. Filsystem notifies sync layer there are new chunks.
5. MetaSyncLayer synchronizes the metadata. SyncLayer seeds chunks.

To read a file:

1. If filesystem already has a copy of the file requested it returns the data directly.
2. Filesystem loads the torrent file and starts searching for a peer with the file data via BitTorrent's distributed hash table (DHT) peer discovery mechanism.
3. Filesystem waits for the blocks covering the read request to become available, and then returns the data to the filesystem client.
4. Repeat per chunk.

To replicate a file:

1. All peers participate in the BitTorrent swarms associated with each file they have local copies of.
2. If a peer notices the catalog falls below a threshold, it will send out clone requests to a subset of its peers until the catalog increases.

# Example Usage

The first time the first node is ever brought up:

```
server1$ delugefs bigstore tank/delugefs --create
```

All future invocations would omit the "--create".

To bring up an additional node on a different disk on the same machine:

```
server1$ ./delugefs.py --cluster bigstore \
    --root /mnt/disk2/.bigstoredb
```

(note the lack of the optional mount point)

To bring up an additional node on a different machine:

```
server2$ delugefs bigstore tank/delugefs --lazy --btport 6881
```

The `--lazy` option means data will only be transfered during a READ, instead of in the background

That's all there is to it!
