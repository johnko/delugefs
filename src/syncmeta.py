'''
This library abstracts the sync of meta data.

It should allow us to switch between:

- git (for remote and local separate disk on same host)
- syncthing

will get calls like

    syncmeta.new(chunk_hash) which copies the torrent from /torrents/ to /new/
    syncmeta.want(chunk_hash) which copies the torrent from /torrents/ to /want/

If we get a new chunk, update our catalog:

    catalog.update()

'''
