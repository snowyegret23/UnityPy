import gc
import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import UnityPy
import pytest

from UnityPy.environment import Environment
from UnityPy.enums import ClassIDType
from UnityPy.files.BundleFile import BlockInfo, BundleFile
from UnityPy.files.ObjectReader import ObjectReader
from UnityPy.files.SerializedFile import SerializedFile
from UnityPy.streams import EndianBinaryReader, EndianBinaryWriter


SAMPLES = Path(__file__).parent / "samples"


def _close_bundle_views(bundle: BundleFile) -> None:
    for entry in list(bundle.files.values()):
        if isinstance(entry, SerializedFile):
            entry.close()
            try:
                entry.reader.dispose()
            except (BufferError, ValueError):
                pass
        elif isinstance(entry, (EndianBinaryReader, EndianBinaryWriter)):
            try:
                entry.dispose()
            except (BufferError, ValueError):
                pass

    bundle.files.clear()
    blocks_reader = getattr(bundle, "_blocks_reader", None)
    if blocks_reader is not None:
        try:
            blocks_reader.dispose()
        except (BufferError, ValueError):
            pass
    bundle._cleanup_temp_blocks_storage()


@pytest.mark.parametrize("packer", ("none", "original", "lz4", "lzma"))
def test_bundle_save_to_matches_save_for_all_packers(tmp_path: Path, packer: str) -> None:
    source_env = UnityPy.load(str(SAMPLES / "atlas_test"))
    source_bundle = source_env.file
    saved_env = None

    try:
        expected = source_bundle.save(packer=packer)
        output_path = tmp_path / f"atlas-{packer}.bundle"
        written_size = source_bundle.save_to(output_path, packer=packer)
        actual = output_path.read_bytes()

        assert written_size == len(actual)
        assert actual == expected

        saved_env = UnityPy.load(actual)
        assert saved_env.file.signature == "UnityFS"
        assert set(saved_env.file.files) == set(source_bundle.files)
    finally:
        if saved_env is not None:
            _close_bundle_views(saved_env.file)
            saved_env.cabs.clear()
            saved_env.files.clear()
        _close_bundle_views(source_bundle)
        source_env.cabs.clear()
        source_env.files.clear()


def test_bundle_save_to_preserves_replaced_raw_entry(tmp_path: Path) -> None:
    source_env = UnityPy.load(str(SAMPLES / "atlas_test"))
    source_bundle = source_env.file
    saved_env = None

    raw_name = next(name for name in source_bundle.files if name.endswith(".resS"))
    original_reader = source_bundle.files[raw_name]
    replacement = b"REPLACED-CONTENT"
    replacement_writer = EndianBinaryWriter(replacement)
    replacement_writer.flags = original_reader.flags
    source_bundle.files[raw_name] = replacement_writer
    original_reader.dispose()
    source_bundle.mark_changed()

    try:
        output_path = tmp_path / "replaced.bundle"
        source_bundle.save_to(output_path)

        saved_env = UnityPy.load(output_path.read_bytes())
        saved_entry = saved_env.file.files[raw_name]
        assert saved_entry.bytes == replacement
    finally:
        if saved_env is not None:
            _close_bundle_views(saved_env.file)
            saved_env.cabs.clear()
            saved_env.files.clear()
        _close_bundle_views(source_bundle)
        source_env.cabs.clear()
        source_env.files.clear()


def test_temp_mmap_cleanup_retries_after_child_view_release() -> None:
    bundle = object.__new__(BundleFile)
    bundle.files = {}
    bundle._blocks_tmp_path = None
    bundle._blocks_tmp_file = None
    bundle._blocks_mmap = None
    bundle._blocks_reader = None
    bundle.decompress_data = lambda data, *_args: bytes(data)

    source_reader = EndianBinaryReader(b"abcdef")
    block_reader = bundle._create_temp_backed_blocks_reader(
        source_reader,
        [BlockInfo(6, 6, 0)],
        offset=0,
    )
    source_reader.dispose()
    bundle._blocks_reader = block_reader
    child_view = block_reader.view[1:4]
    temp_path = Path(bundle._blocks_tmp_path)
    bundle_file_module = importlib.import_module("UnityPy.files.BundleFile")
    real_remove = os.remove
    deletion_blocked = True

    def windows_like_remove(path) -> None:
        if Path(path) == temp_path and deletion_blocked:
            raise PermissionError("mapped file is still in use")
        real_remove(path)

    try:
        with mock.patch.object(bundle_file_module.os, "remove", side_effect=windows_like_remove):
            bundle._cleanup_temp_blocks_storage()
            if os.name == "nt":
                assert temp_path.exists()

            child_view.release()
            child_view = None
            block_reader.dispose()
            block_reader = None
            deletion_blocked = False
            gc.collect()

            bundle._cleanup_temp_blocks_storage()

        assert not temp_path.exists()
    finally:
        if child_view is not None:
            child_view.release()
        if block_reader is not None:
            try:
                block_reader.dispose()
            except (BufferError, ValueError):
                pass
        if temp_path.exists():
            real_remove(temp_path)


def test_get_raw_data_returns_current_in_memory_replacement() -> None:
    replacements = (
        b"bytes replacement",
        bytearray(b"bytearray replacement"),
        memoryview(b"memoryview replacement"),
    )

    for replacement in replacements:
        reader = EndianBinaryReader(b"original")
        assets_file = SimpleNamespace(mark_changed=mock.Mock())
        obj_reader = ObjectReader(
            assets_file=assets_file,
            reader=reader,
            path_id=7,
            type_id=28,
            serialized_type=None,
            class_id=int(ClassIDType.Texture2D),
            type=ClassIDType.Texture2D,
            byte_start=0,
            byte_size=len(b"original"),
            is_destroyed=None,
            is_stripped=None,
        )

        try:
            obj_reader.set_raw_data(replacement)
            assert obj_reader.get_raw_data() == bytes(replacement)
        finally:
            reader.dispose()


def test_environment_save_prefers_save_to(tmp_path: Path) -> None:
    item = mock.Mock(spec_set=["is_changed", "save", "save_to"])
    item.is_changed = True
    item.save.return_value = b"legacy-save"

    def save_to(path, packer=None):
        Path(path).write_bytes(b"streamed-save")
        return len(b"streamed-save")

    item.save_to.side_effect = save_to
    env = Environment()
    env.files = {"nested/input.bundle": item}

    env.save(pack="lz4", out_path=str(tmp_path))

    item.save.assert_not_called()
    item.save_to.assert_called_once()
    call = item.save_to.call_args
    assert Path(call.args[0]) == tmp_path / "input.bundle"
    packer = call.kwargs.get("packer", call.args[1] if len(call.args) > 1 else None)
    assert packer == "lz4"
    assert (tmp_path / "input.bundle").read_bytes() == b"streamed-save"
