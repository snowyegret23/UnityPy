import unittest
from types import SimpleNamespace

from UnityPy.environment import Environment
from UnityPy.files.SerializedFile import SerializedFile
from UnityPy.helpers.ContainerHelper import ContainerHelper


def make_serialized_file(container):
    serialized = object.__new__(SerializedFile)
    serialized._container = container
    serialized._spill_store = None
    return serialized


class RegisteringContainer:
    def __init__(self, environment, added_file):
        self.environment = environment
        self.added_file = added_file
        self.calls = 0
        self.pending = True

    def parse_preload_table(self):
        self.calls += 1
        if self.pending:
            self.pending = False
            self.environment.register_cab("added", self.added_file)


class TrackingContainer:
    def __init__(self):
        self.calls = 0

    def parse_preload_table(self):
        self.calls += 1


class Pointer:
    def __init__(self, path_id, target=None):
        self.path_id = path_id
        self.target = target

    def __bool__(self):
        return self.path_id != 0

    def deref(self):
        return self.target


class ContainerPreloadRegressionTests(unittest.TestCase):
    def test_container_index_handles_cab_registration_during_iteration(self):
        environment = Environment()
        added_container = TrackingContainer()
        added_file = make_serialized_file(added_container)
        registering_container = RegisteringContainer(environment, added_file)
        environment.register_cab("first", make_serialized_file(registering_container))

        environment._build_container_index()

        self.assertEqual(registering_container.calls, 1)
        self.assertEqual(added_container.calls, 0)
        self.assertFalse(environment._container_index_built)

        environment._build_container_index()

        self.assertEqual(added_container.calls, 1)
        self.assertTrue(environment._container_index_built)

    def test_parse_preload_table_maps_target_path_ids(self):
        target_container = ContainerHelper([])
        target_container.path_dict[202] = "existing/path"
        target_file = SimpleNamespace(_container=target_container)
        target = SimpleNamespace(assets_file=target_file)

        direct_asset = Pointer(101)
        info = SimpleNamespace(asset=direct_asset, preloadIndex=0, preloadSize=3)
        bundle = SimpleNamespace(
            m_Container=[("assets/main", info)],
            m_PreloadTable=[Pointer(201, target), Pointer(202, target), Pointer(0)],
        )
        helper = ContainerHelper(bundle)

        helper.parse_preload_table()

        self.assertEqual(target_container.path_dict[201], "assets/main")
        self.assertEqual(target_container.path_dict[202], "existing/path")
        self.assertIsNone(helper._preload_table)


if __name__ == "__main__":
    unittest.main()
