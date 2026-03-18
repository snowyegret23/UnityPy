import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from UnityPy.enums import ClassIDType
from UnityPy.files.ObjectReader import ObjectReader, _SPILL_RAW_DATA_THRESHOLD
from UnityPy.files.replacers import AppendOnlySpillStore, SpillStoreSliceReplacer
from UnityPy.streams.EndianBinaryWriter import EndianBinaryWriter


class DummyAssetsFile:
    def __init__(self) -> None:
        self.mark_changed_calls = 0
        self.big_id_enabled = False
        self._spill_store = None

    def mark_changed(self) -> None:
        self.mark_changed_calls += 1

    def get_spill_store(self):
        if self._spill_store is None:
            self._spill_store = AppendOnlySpillStore(prefix="unitypy_test_spill_")
        return self._spill_store

    def close(self) -> None:
        if self._spill_store is not None:
            self._spill_store.close()
            self._spill_store = None


class ObjectReaderSpillTests(unittest.TestCase):
    def _make_reader(self, *, byte_size: int) -> ObjectReader[object]:
        assets_file = DummyAssetsFile()
        self.addCleanup(assets_file.close)
        reader = SimpleNamespace(endian=">", Position=0)
        return ObjectReader(
            assets_file=assets_file,
            reader=reader,
            path_id=7,
            type_id=28,
            serialized_type=None,
            class_id=int(ClassIDType.Texture2D),
            type=ClassIDType.Texture2D,
            byte_start=0,
            byte_size=byte_size,
            is_destroyed=None,
            is_stripped=None,
        )

    def test_save_typetree_spills_large_payload(self) -> None:
        payload = b"A" * (_SPILL_RAW_DATA_THRESHOLD + 1024)
        obj_reader = self._make_reader(byte_size=len(payload))

        with mock.patch.object(
            ObjectReader,
            "_get_typetree_node",
            return_value=object(),
        ), mock.patch(
            "UnityPy.files.ObjectReader.TypeTreeHelper.write_typetree",
            side_effect=lambda tree, node, writer, assets_file: writer.write(payload),
        ):
            result = obj_reader.save_typetree({"dummy": True})

        self.assertIsInstance(result, SpillStoreSliceReplacer)
        self.assertIsInstance(obj_reader.data, SpillStoreSliceReplacer)
        self.assertEqual(len(payload), len(result))
        self.assertEqual(payload, obj_reader.get_raw_data())

        header_writer = EndianBinaryWriter()
        data_writer = EndianBinaryWriter()
        obj_reader.write(SimpleNamespace(version=22), header_writer, data_writer)
        self.assertEqual(payload, data_writer.bytes)

        spilled_path = obj_reader.assets_file.get_spill_store().path
        obj_reader.set_raw_data(b"small")
        self.assertTrue(os.path.exists(spilled_path))

    def test_save_typetree_keeps_small_payload_in_memory(self) -> None:
        payload = b"B" * 1024
        obj_reader = self._make_reader(byte_size=len(payload))

        with mock.patch.object(
            ObjectReader,
            "_get_typetree_node",
            return_value=object(),
        ), mock.patch(
            "UnityPy.files.ObjectReader.TypeTreeHelper.write_typetree",
            side_effect=lambda tree, node, writer, assets_file: writer.write(payload),
        ):
            result = obj_reader.save_typetree({"dummy": True})

        self.assertIsInstance(result, bytes)
        self.assertEqual(payload, result)
        self.assertEqual(payload, obj_reader.data)


if __name__ == "__main__":
    unittest.main()
