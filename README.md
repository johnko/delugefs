This fork is brought to a simmer over medium-low heat, and modified to for FreeBSD.

# This Fork's Planned Changes

- change the "keep_pushing" loop to event driven?? but loses separate thread
- SSH instead of JSONRPC - blocker is that please_mirror, please_stop_mirroring, active_torrents and freespace currently depends on these
- though local network peers are auto-discovered, still need to manually add SSH pubkeys to one existing cluster node
- possibility to manually add external network peer
- FUSE mounted allow_other

# This Fork's Implemented Changes

Tick if tested:

- [ ] specify BitTorrent start port number with --port
- [ ] Git instead of Mercurial
- [ ] SSH prep-node script to bootstrap id_ed25519.pub and known_hosts
- [ ] force SSH command in authorized_keys2 after "git push", "git merge tomerge" ... command="git-shell -c $SSH_ORIGINAL_COMMAND"

# Overview

DelugeFS is a distributed filesystem [@keredson](https://github.com/keredson) implemented as a proof of concept of several ideas he's had over the past few years.

Key features include:

- a [shared-nothing architecture](http://en.wikipedia.org/wiki/Shared_nothing_architecture) (meaning there is no master node controlling everything - all peers are equal and there is no single point of failure)
- auto-discovery of local network peers
- able to utilize highly heterogeneous computational resources (ie: a random bunch of disks stuck in a random number of machines)

Key insights this FS proves:

- Efficient distribution of large blocks of immutable data without centralized control is a solved problem. [libtorrent](http://www.rasterbar.com/products/libtorrent/).
- Efficient sharing of small quantities of mutable data (without a central server) is a solved problem. [Mercurial](http://mercurial.selenic.com/), or [Git](http://git-scm.com/).
- Automatic discovery of peers on a local network is a solved problem. [Zeroconf/Bonjour](http://en.wikipedia.org/wiki/Zeroconf).
- Emulating a filesystem is a solved problem with [FUSE](http://en.wikipedia.org/wiki/Filesystem_in_Userspace).
- All of these projects have Python bindings!
- The key node in a distributed filesystem is the _disk_, not the machine. Everything above the disk is network topology.

# Requirements

FreeBSD:

- kldload fuse
- pkg install python27 libffi indexinfo gettext-runtime py27-setuptools27
- pkg install fusefs-libs
- pkg install py27-pybonjour mDNSResponder
- pkg install py27-libtorrent-rasterbar libtorrent-rasterbar GeoIP boost-libs icu boost-python-libs
- pkg install py27-sh


# Current Status

**HIGHLY EXPERIMENTAL! -- PROOF OF CONCEPT ONLY -- DO NOT USE FOR ANY CRITICAL DATA AT THIS POINT!**

In 2013 [@keredson](https://github.com/keredson) was using it as personal media center storage spanning three disks on two machines. It works well so far, but it still very early in development.

Speed:

- I/O across the FUSE boundary is CPU limited. Max observed is ~10MB/s. [@keredson](https://github.com/keredson) suspects this is a limitation of the Python FUSE bindings.
- I/O between nodes is limited by the disk read/write speeds. [@keredson](https://github.com/keredson) has observed >70MB/s sustained on his home network.

## Known Issues

- Files over ~4GB are not stored (and their zero-length stubs cannot be deleted). [@keredson](https://github.com/keredson) believes this is due to an int vs. long incompatibility with libtorrent, but hasn't confirmed.

# Basic Algorithm

To start up:

1. Filesystem is started given a volume id, a storage location, and a mount point.
2. Filesystem searches for local peers.
3. Filesystem either pulls from our clones other peer's repositories.
4. Filesystem looks for any files it has locally (complete or not), and starts seeding them.

To write a file:

1. Filesystem client opens a file and attempts to write. Filesystem returns a handle to a local temporary file.
2. Client writes to file and then closes it.
3. Filesystem create a torrent of that file (containing metadata about the file along with secure hashes of its contents) and commits it to a local repository.
4. Filesystem contacts local peers and sends them a pull request.
5. Other peers pull the file metadata update.

To read a file:

1. If filesystem already has a copy of the file requested it returns the data directly.
2. Filesystem loads the torrent file and starts searching for a peer with the file data via BitTorrent's distributed hash table (DHT) peer discovery mechanism.
3. Filesystem waits for the blocks covering the read request to become available, and then returns the data to the filesystem client.

To replicate a file:

1. All peers participate in the BitTorrent swarms associated with each file they have local copies of.
2. If a peer notices the swarm size falls below a threshold, it will send out clone requests to a subset of its peers until the swarm size increases.

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
server2$ delugefs bigstore tank/delugefs
```

That's all there is to it!
