"""A Farmhand drive carries a goal point, not a provider marker or route ID."""
import unittest

from protocol.ipc import encode_command


class DriveContractTests(unittest.TestCase):
    def test_goal_point_has_only_the_selected_vehicle_and_coordinates(self):
        xml = encode_command("session-1", 7, "farm_drive", {"tractor": 611, "x": -759.0, "z": 129.0, "area": 40.0,
                                                             "yaw": 10.0})
        self.assertIn('op="farm_drive"', xml)
        self.assertIn('tractor="611"', xml)
        self.assertNotIn("marker", xml)

    def test_marker_cannot_be_smuggled_into_a_farmhand_drive(self):
        with self.assertRaises(ValueError):
            encode_command("session-1", 7, "farm_drive",
                           {"tractor": 611, "x": 1.0, "z": 2.0, "area": 0.0, "yaw": 10.0, "marker": 3})


if __name__ == "__main__":
    unittest.main()
