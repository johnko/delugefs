'''
This library abstracts the splitting of a file to chunks of X size and addressing using Y secure hash function.
And reading a file that consists of chunks.

On a write op:

    It should store the chunks to

        program_root/chunks/(hash depth*width)/chunk_hash

    A .torrent file is created for each chunk it wrote and store the .torrent file in

        program_root/meta/torrents/(hash depth*width)/chunk_hash.torrent

    Also create an index that is stored at

        program_root/meta/index/original_path/original_file_name

    The index is a plaintext with:
        chunk1_hash chunk1_size
        chunk2_hash chunk2_size
        chunk3_hash chunk3_size
        ...
        chunkn_hash chunkn_size

    Finally, notify:
        syncmeta.new(index/original_path/original_file_name)
        syncmeta.new(torrents/(hash depth*width)/chunk_hash.torrent)

On a read op:

    It should read the index at

        program_root/meta/index/original_path/original_file_name

    and for each chunk, determine if it has it or not.
    It can optionally verify the chunk_hash matches the content of the chunk.

    If it does not exist, call syncchunk.fetch(chunk_hash)
    (which will start torrent from the following if we have torrent peers)

        program_root/meta/torrents/(hash depth*width)/chunk_hash.torrent

    Finally return the data that was stored at

        program_root/chunks/(hash depth*width)/chunk_hash

'''
