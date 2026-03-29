# TODO: implement encryption for saving files
import io
import mmap
import os
import re
import tempfile
from collections import namedtuple
from typing import Optional, Union, cast

from .. import config
from ..enums import ArchiveFlags, ArchiveFlagsOld, CompressionFlags
from ..helpers import ArchiveStorageManager, CompressionHelper
from ..helpers.UnityVersion import UnityVersion
from ..streams import EndianBinaryReader, EndianBinaryWriter
from . import File
from .replacers import BytesReplacer, Replacer, SourceSliceReplacer, TempFileReplacer

BlockInfo = namedtuple("BlockInfo", "uncompressedSize compressedSize flags")
DirectoryInfoFS = namedtuple("DirectoryInfoFS", "offset size flags path")
reVersion = re.compile(r"(\d+)\.(\d+)\.(\d+)\w.+")


class BundleFile(File.File):
    format: int
    is_changed: bool
    signature: str
    version_engine: str
    version_player: str
    dataflags: Union[ArchiveFlags, ArchiveFlagsOld]
    decryptor: Optional[ArchiveStorageManager.ArchiveStorageDecryptor] = None
    _uses_block_alignment: bool = False
    _block_info_flags: int = 0
    _blocks_tmp_path: Optional[str] = None
    _blocks_tmp_file = None
    _blocks_mmap = None
    _blocks_reader: Optional[EndianBinaryReader] = None
    _directory_info_map: dict[str, DirectoryInfoFS]

    def __init__(
        self,
        reader: EndianBinaryReader,
        parent: File,
        name: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(parent=parent, name=name, **kwargs)
        signature = self.signature = reader.read_string_to_null()
        self.version = reader.read_u_int()
        self.version_player = reader.read_string_to_null()
        self.version_engine = reader.read_string_to_null()

        if signature == "UnityArchive":
            raise NotImplementedError("BundleFile - UnityArchive")
        elif signature in ["UnityWeb", "UnityRaw"]:
            if self.version == 6:
                m_DirectoryInfo, blocksReader = self.read_fs(reader)
            else:
                m_DirectoryInfo, blocksReader = self.read_web_raw(reader)
        elif signature == "UnityFS":
            m_DirectoryInfo, blocksReader = self.read_fs(reader)
        else:
            raise NotImplementedError(f"Unknown Bundle signature: {signature}")

        self._blocks_reader = blocksReader
        self._directory_info_map = {info.path: info for info in m_DirectoryInfo}
        self.read_files(blocksReader, m_DirectoryInfo)

    def _cleanup_temp_blocks_storage(self):
        mmap_obj = getattr(self, "_blocks_mmap", None)
        file_obj = getattr(self, "_blocks_tmp_file", None)
        tmp_path = getattr(self, "_blocks_tmp_path", None)

        self._blocks_reader = None
        self._blocks_mmap = None
        self._blocks_tmp_file = None
        self._blocks_tmp_path = None

        if mmap_obj is not None:
            try:
                mmap_obj.close()
            except (BufferError, OSError, ValueError):
                pass

        if file_obj is not None:
            try:
                file_obj.close()
            except OSError:
                pass

        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def __del__(self):
        self._cleanup_temp_blocks_storage()

    def _create_temp_backed_blocks_reader(
        self,
        reader: EndianBinaryReader,
        blocks_info: list[BlockInfo],
        *,
        offset: int,
    ):
        self._cleanup_temp_blocks_storage()

        tmp_fd, tmp_path = tempfile.mkstemp(prefix="UnityPy_bundle_blocks_", suffix=".tmp")
        tmp_file = os.fdopen(tmp_fd, "w+b")
        try:
            for index, block_info in enumerate(blocks_info):
                decompressed_block = self.decompress_data(
                    reader.read_bytes(block_info.compressedSize),
                    block_info.uncompressedSize,
                    block_info.flags,
                    index,
                )
                if decompressed_block is not None:
                    tmp_file.write(decompressed_block)
            tmp_file.flush()
            tmp_file.seek(0)
            if tmp_file.seek(0, os.SEEK_END) == 0:
                tmp_file.close()
                os.remove(tmp_path)
                return EndianBinaryReader(b"", offset=offset)
            tmp_file.seek(0)
            mmap_obj = mmap.mmap(tmp_file.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            try:
                tmp_file.close()
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

        self._blocks_tmp_path = tmp_path
        self._blocks_tmp_file = tmp_file
        self._blocks_mmap = mmap_obj
        return EndianBinaryReader(memoryview(mmap_obj), offset=offset)

    def read_web_raw(self, reader: EndianBinaryReader):
        # def read_header_and_blocks_info(self, reader:EndianBinaryReader):
        version = self.version
        if version >= 4:
            self._hash = reader.read_bytes(16)
            self.crc = reader.read_u_int()

        minimumStreamedBytes = reader.read_u_int()  # noqa: F841
        headerSize = reader.read_u_int()
        numberOfLevelsToDownloadBeforeStreaming = reader.read_u_int()  # noqa: F841
        levelCount = reader.read_int()
        reader.Position += 4 * 2 * (levelCount - 1)

        compressedSize = reader.read_u_int()
        uncompressedSize = reader.read_u_int()  # noqa: F841

        if version >= 2:
            completeFileSize = reader.read_u_int()  # noqa: F841

        if version >= 3:
            fileInfoHeaderSize = reader.read_u_int()  # noqa: F841

        reader.Position = headerSize

        uncompressedBytes = reader.read_bytes(compressedSize)
        if self.signature == "UnityWeb":
            uncompressedBytes = CompressionHelper.decompress_lzma(uncompressedBytes, True)

        blocksReader = EndianBinaryReader(uncompressedBytes, offset=headerSize)
        nodesCount = blocksReader.read_int()
        m_DirectoryInfo = [
            File.DirectoryInfo(
                blocksReader.read_string_to_null(),  # path
                blocksReader.read_u_int(),  # offset
                blocksReader.read_u_int(),  # size
            )
            for _ in range(nodesCount)
        ]

        return m_DirectoryInfo, blocksReader

    def read_fs(self, reader: EndianBinaryReader):
        size = reader.read_long()  # noqa: F841

        # header
        compressedSize = reader.read_u_int()
        uncompressedSize = reader.read_u_int()
        dataflagsValue = reader.read_u_int()

        # UnityWeb version 6
        if self.signature != "UnityFS":
            reader.read_byte()

        version = self.parse_version()
        # https://issuetracker.unity3d.com/issues/files-within-assetbundles-do-not-start-on-aligned-boundaries-breaking-patching-on-nintendo-switch
        # Unity CN introduced encryption before the alignment fix was introduced.
        # Unity CN used the same flag for the encryption as later on the alignment fix,
        # so we have to check the version to determine the correct flag set.
        if (
            version < (2020,)
            or (version[0] == 2020 and version < (2020, 3, 34))
            or (version[0] == 2021 and version < (2021, 3, 2))
            or (version[0] == 2022 and version < (2022, 1, 1))
        ):
            self.dataflags = ArchiveFlagsOld(dataflagsValue)
        else:
            self.dataflags = ArchiveFlags(dataflagsValue)

        if self.dataflags & self.dataflags.UsesAssetBundleEncryption:
            self.decryptor = ArchiveStorageManager.ArchiveStorageDecryptor(reader)

        # if header version is 7 or later we need to align the reader
        # for 2019.4.15 and later, version should be 7 and aligned
        # but some games in these versions somehow has version 6 while aligned
        if self.version >= 7 or (version[0] == 2019 and version >= (2019, 4, 15)):
            reader.align_stream(16)
            self._uses_block_alignment = True

        start = reader.Position
        if self.dataflags & ArchiveFlags.BlocksInfoAtTheEnd:  # kArchiveBlocksInfoAtTheEnd
            reader.Position = reader.Length - compressedSize
            blocksInfoBytes = reader.read_bytes(compressedSize)
            reader.Position = start
        else:  # 0x40 kArchiveBlocksAndDirectoryInfoCombined
            blocksInfoBytes = reader.read_bytes(compressedSize)

        blocksInfoBytes = self.decompress_data(blocksInfoBytes, uncompressedSize, self.dataflags)
        blocksInfoReader = EndianBinaryReader(blocksInfoBytes, offset=start)

        uncompressedDataHash = blocksInfoReader.read_bytes(16)  # noqa: F841
        blocksInfoCount = blocksInfoReader.read_int()

        m_BlocksInfo = [
            BlockInfo(
                blocksInfoReader.read_u_int(),  # uncompressedSize
                blocksInfoReader.read_u_int(),  # compressedSize
                blocksInfoReader.read_u_short(),  # flags
            )
            for _ in range(blocksInfoCount)
        ]

        nodesCount = blocksInfoReader.read_int()
        m_DirectoryInfo = [
            DirectoryInfoFS(
                blocksInfoReader.read_long(),  # offset
                blocksInfoReader.read_long(),  # size
                blocksInfoReader.read_u_int(),  # flags
                blocksInfoReader.read_string_to_null(),  # path
            )
            for _ in range(nodesCount)
        ]

        if m_BlocksInfo:
            self._block_info_flags = m_BlocksInfo[0].flags

        if isinstance(self.dataflags, ArchiveFlags) and self.dataflags & ArchiveFlags.BlockInfoNeedPaddingAtStart:
            reader.align_stream(16)

        blocksReader = self._create_temp_backed_blocks_reader(
            reader,
            m_BlocksInfo,
            offset=(blocksInfoReader.real_offset()),
        )

        return m_DirectoryInfo, blocksReader

    def _write_header(self, writer: EndianBinaryWriter):
        """Write the common bundle file header fields."""
        writer.write_string_to_null(self.signature)
        writer.write_u_int(self.version)
        writer.write_string_to_null(self.version_player)
        writer.write_string_to_null(self.version_engine)

    def _dispatch_save(self, writer: EndianBinaryWriter, packer=None):
        """Dispatch save to the correct format handler based on signature and packer."""
        if self.signature == "UnityArchive":
            raise NotImplementedError("BundleFile - UnityArchive")
        elif self.signature in ["UnityWeb", "UnityRaw"]:
            if self.version == 6:
                self.save_fs(writer, 64, 64)
            else:
                self.save_web_raw(writer)
        elif self.signature == "UnityFS":
            if not packer or packer == "none":
                self.save_fs(writer, 64, 64)
            elif packer == "original":
                self.save_fs(
                    writer,
                    data_flag=self.dataflags,
                    block_info_flag=self._block_info_flags,
                )
            elif packer == "lz4":
                self.save_fs(writer, data_flag=194, block_info_flag=2)
            elif packer == "lzma":
                self.save_fs(writer, data_flag=65, block_info_flag=1)
            elif isinstance(packer, tuple):
                self.save_fs(writer, *packer)
            else:
                raise NotImplementedError("UnityFS - Packer:", packer)

    def save(self, packer=None):
        """
        Rewrites the BundleFile and returns it as bytes object.

        packer:
            can be either one of the following strings
            or tuple consisting of (block_info_flag, data_flag)
            allowed strings:
                none - no compression, default, safest bet
                lz4 - lz4 compression
                original - uses the original flags
        """
        writer = EndianBinaryWriter()
        self._write_header(writer)
        self._dispatch_save(writer, packer)
        return writer.bytes

    def save_to(self, path, packer=None):
        """Save directly to a file, avoiding the final in-memory bytes copy.

        Returns the size of the written file in bytes.

        packer: same options as save().
        """
        with open(path, "wb") as f:
            writer = EndianBinaryWriter(f)
            self._write_header(writer)
            self._dispatch_save(writer, packer)

        return os.path.getsize(path)

    def save_fs(self, writer: EndianBinaryWriter, data_flag: int, block_info_flag: int):
        # Detect whether the writer is backed by a real file (save_to)
        # or an in-memory BytesIO (save).  When file-backed we can use
        # temp files to avoid holding multi-GB compressed payloads in RAM.
        is_file_backed = not isinstance(writer.stream, io.BytesIO)

        files = []
        temp_replacers: list[TempFileReplacer] = []

        def build_replacer(name: str, f) -> Replacer:
            original_info = self._directory_info_map.get(name)
            if (
                original_info is not None
                and self._blocks_reader is not None
                and not getattr(f, "is_changed", False)
            ):
                return SourceSliceReplacer.from_reader(
                    self._blocks_reader,
                    int(original_info.offset),
                    int(original_info.size),
                )

            if isinstance(f, EndianBinaryReader):
                return SourceSliceReplacer.from_reader(f, 0, f.Length)

            if isinstance(f, EndianBinaryWriter):
                return SourceSliceReplacer.from_writer(f)

            if is_file_backed and hasattr(f, "save_to"):
                tmp_fd, tmp_path = tempfile.mkstemp(prefix="unitypy_bundle_entry_", suffix=".bin")
                os.close(tmp_fd)
                f.save_to(tmp_path)
                replacer = TempFileReplacer(
                    tmp_path,
                    os.path.getsize(tmp_path),
                    delete_on_cleanup=True,
                )
                temp_replacers.append(replacer)
                return replacer

            return BytesReplacer(f.save())

        file_entries: list[tuple[str, int, Replacer]] = []
        for name, f in self.files.items():
            replacer = build_replacer(name, f)
            file_entries.append((name, getattr(f, "flags", 0), replacer))
            files.append((name, getattr(f, "flags", 0), len(replacer)))

        def iter_file_data():
            for _name, _flags, replacer in file_entries:
                yield from replacer.iter_chunks()

        # remove encryption flag, as encryption is not applied by UnityPy during save
        if block_info_flag & self.dataflags.UsesAssetBundleEncryption:
            block_info_flag ^= self.dataflags.UsesAssetBundleEncryption
        if data_flag & self.dataflags.UsesAssetBundleEncryption:
            data_flag ^= self.dataflags.UsesAssetBundleEncryption

        # Compress file payloads.  When file-backed, write compressed
        # output to a temp file to avoid a huge in-memory bytearray.
        compressed_tmp_path = None
        if is_file_backed:
            _cfd, compressed_tmp_path = tempfile.mkstemp()
            os.close(_cfd)
            block_info = CompressionHelper.chunk_based_compress_iter_to_file(
                iter_file_data(), block_info_flag, compressed_tmp_path
            )
            compressed_data_size = os.path.getsize(compressed_tmp_path)
        else:
            file_data, block_info = CompressionHelper.chunk_based_compress_iter(
                iter_file_data(), block_info_flag
            )
            compressed_data_size = len(file_data)

        try:
            # write the block_info
            # uncompressedDataHash
            block_writer = EndianBinaryWriter(b"\x00" * 0x10)
            # data block info
            block_writer.write_int(len(block_info))
            for block_uncompressed_size, block_compressed_size, block_flag in block_info:
                block_writer.write_u_int(block_uncompressed_size)
                block_writer.write_u_int(block_compressed_size)
                block_writer.write_u_short(block_flag)

            # file block info
            if not data_flag & 0x40:
                raise NotImplementedError("UnityPy always writes DirectoryInfo, so data_flag must include 0x40")
            block_writer.write_int(len(files))
            offset = 0
            for f_name, f_flag, f_len in files:
                block_writer.write_long(offset)
                block_writer.write_long(f_len)
                offset += f_len
                block_writer.write_u_int(f_flag)
                block_writer.write_string_to_null(f_name)

            # compress the block data
            block_data = block_writer.bytes
            block_writer.dispose()

            uncompressed_block_data_size = len(block_data)

            switch = data_flag & 0x3F
            if switch in CompressionHelper.COMPRESSION_MAP:
                block_data = CompressionHelper.COMPRESSION_MAP[switch](block_data)
            else:
                raise NotImplementedError(
                    f"No compression function in the CompressionHelper.COMPRESSION_MAP for {switch}"
                )

            compressed_block_data_size = len(block_data)

            # write the header info
            writer_header_pos = writer.Position
            writer.write_long(0)
            writer.write_u_int(compressed_block_data_size)
            writer.write_u_int(uncompressed_block_data_size)
            writer.write_u_int(data_flag)

            # UnityWeb version 6
            if self.signature != "UnityFS":
                writer.write_byte(0)

            if self._uses_block_alignment:
                writer.align_stream(16)

            def _write_file_data():
                """Write the compressed file data to writer."""
                if compressed_tmp_path:
                    with open(compressed_tmp_path, "rb") as cf:
                        while True:
                            chunk = cf.read(1048576)
                            if not chunk:
                                break
                            writer.write(chunk)
                else:
                    writer.write(file_data)

            if data_flag & 0x80:  # at end of file
                if data_flag & 0x200:
                    writer.align_stream(16)
                _write_file_data()
                writer.write(block_data)
            else:
                writer.write(block_data)
                if data_flag & 0x200:
                    writer.align_stream(16)
                _write_file_data()

            writer_end_pos = writer.Position
            writer.Position = writer_header_pos
            # correct file size
            writer.write_long(writer_end_pos)
            writer.Position = writer_end_pos
        finally:
            for replacer in temp_replacers:
                replacer.cleanup()
            if compressed_tmp_path:
                try:
                    os.remove(compressed_tmp_path)
                except OSError:
                    pass

    def save_web_raw(self, writer: EndianBinaryWriter):
        # (version >= 4) hash
        # (version >= 4) crc
        # minimumStreamedBytes
        # headerSize
        # numberOfLevelsToDownloadBeforeStreaming
        # levelCount
        # compressedSize * levelCount
        # uncompressedSize * levelCount
        # (version >= 2) completeFileSize
        # (version >= 3) file_info_header_size
        # compressed assets

        if self.version > 3:
            raise NotImplementedError("Saving Unity Web bundles with version > 3 is not supported")

        # Calculate fileInfoHeaderSize for set offsets
        file_info_header_size = 4  # for nodesCount

        for file_name in self.files.keys():
            file_info_header_size += len(file_name.encode()) + 1  # +1 for null terminator
            file_info_header_size += 4 * 2  # 4 bytes each for offset and size

        file_info_header_padding_size = 4 - (file_info_header_size % 4) if file_info_header_size % 4 != 0 else 0
        file_info_header_size += file_info_header_padding_size

        # Prepare directory info
        directory_info_writer = EndianBinaryWriter()
        directory_info_writer.write_int(len(self.files))  # nodesCount

        file_content_writer = EndianBinaryWriter()
        current_offset = file_info_header_size

        for file_name, f in self.files.items():
            directory_info_writer.write_string_to_null(file_name)
            directory_info_writer.write_u_int(current_offset)

            # Get file content
            if isinstance(f, (EndianBinaryReader, EndianBinaryWriter)):
                file_data = f.bytes
            else:
                file_data = f.save()

            file_size = len(file_data)
            directory_info_writer.write_u_int(file_size)

            file_content_writer.write_bytes(file_data)
            current_offset += file_size

        directory_info_writer.write(b"\x00" * file_info_header_padding_size)
        uncompressed_directory_info = directory_info_writer.bytes
        directory_info_writer.dispose()
        uncompressed_file_content = file_content_writer.bytes
        file_content_writer.dispose()

        # Combine directory info and file content
        uncompressed_content = uncompressed_directory_info + uncompressed_file_content
        compressed_content = uncompressed_content
        if self.signature == "UnityWeb":
            compressed_content = CompressionHelper.compress_lzma(uncompressed_content, True)

        # Write header
        header_size = writer.Position + 24  # assuming levelCount = 1
        if self.version >= 2:
            header_size += 4
        if self.version >= 3:
            header_size += 4
        if self.version >= 4:
            header_size += 20
        # pad to multiple of 4
        header_size = (header_size + 3) & ~3

        if self.version >= 4:
            writer.write_bytes(self._hash)
            writer.write_u_int(self.crc)

        writer.write_u_int(header_size + len(compressed_content))  # minimumStreamedBytes (same as completeFileSize)
        writer.write_u_int(header_size)  # headerSize
        writer.write_u_int(1)  # numberOfLevelsToDownloadBeforeStreaming (always 1)
        writer.write_int(1)  # levelCount (always 1)

        writer.write_u_int(len(compressed_content))  # compressedSize
        writer.write_u_int(len(uncompressed_content))  # uncompressedSize

        if self.version >= 2:
            writer.write_u_int(header_size + len(compressed_content))  # completeFileSize

        if self.version >= 3:
            writer.write_u_int(file_info_header_size)  # file_info_header_size

        # align header
        writer.align_stream(4)

        # Write compressed content
        writer.write(compressed_content)

    def decompress_data(
        self,
        compressed_data: bytes,
        uncompressed_size: int,
        flags: Union[int, ArchiveFlags, ArchiveFlagsOld],
        index: int = 0,
    ) -> bytes:
        """
        Parameters
        ----------
        compressed_data : bytes
            The compressed data.
        uncompressed_size : int
            The uncompressed size of the data.
        flags : int
            The flags of the data.

        Returns
        -------
        bytes
            The decompressed data."""
        comp_flag = CompressionFlags(flags & ArchiveFlags.CompressionTypeMask)

        if self.decryptor is not None and flags & 0x100 and comp_flag != CompressionFlags.NONE:
            compressed_data = self.decryptor.decrypt_block(compressed_data, index)

        if comp_flag in CompressionHelper.DECOMPRESSION_MAP:
            return cast(
                bytes,
                CompressionHelper.DECOMPRESSION_MAP[comp_flag](compressed_data, uncompressed_size),
            )
        else:
            raise ValueError(f"Unknown compression! flag: {flags}, compression flag: {comp_flag.value}")

    def parse_version(self) -> UnityVersion:
        """Returns the version as a tuple."""
        version = None
        version_str = self.version_engine
        try:
            version = UnityVersion.from_str(version_str)
        except ValueError:
            pass

        if version is None or version.major == 0:
            version_str = config.get_fallback_version()
            version = UnityVersion.from_str(version_str)

        return version
