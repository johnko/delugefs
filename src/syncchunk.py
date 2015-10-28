'''
This library abstracts the sync of meta data.

It should allow us to switch between:

- SSL bittorrent (libtorrent-rasterbar)
- rsync
- HTTP

will get calls like

    syncchunk.fetch(chunk_hash)
    syncchunk.serve(chunk_hash)

We must determine the appropriate node and protocol to fetch with.

If torrent peer, the easy way is to call syncmeta.want(chunk_hash)

'''
