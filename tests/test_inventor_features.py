import math
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class FakeFastMCP:
    def __init__(self, _name):
        pass

    def tool(self):
        return lambda function: function

    def run(self):
        pass


fake_mcp = types.ModuleType("mcp")
fake_mcp_server = types.ModuleType("mcp.server")
fake_fastmcp = types.ModuleType("mcp.server.fastmcp")
fake_fastmcp.FastMCP = FakeFastMCP
sys.modules.setdefault("mcp", fake_mcp)
sys.modules.setdefault("mcp.server", fake_mcp_server)
sys.modules.setdefault("mcp.server.fastmcp", fake_fastmcp)

import inventor_mcp_server as server


class InventorFeatureParsingTests(unittest.TestCase):
    def test_3d_path_parser_accepts_finite_connected_points(self):
        self.assertEqual(
            server._parse_3d_point_string("0,0,0; 10, 0, 0; 10,20,5"),
            [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 20.0, 5.0)],
        )

    def test_3d_path_parser_rejects_duplicate_neighbours(self):
        with self.assertRaisesRegex(ValueError, "Consecutive path points"):
            server._parse_3d_point_string("0,0,0;0,0,0")

    def test_normal_plane_axes_are_orthonormal_to_the_path(self):
        start = (1.0, 2.0, 3.0)
        end = (5.0, 7.0, 9.0)
        x_axis, y_axis = server._normal_plane_axes(start, end)
        path = tuple(end[index] - start[index] for index in range(3))

        dot_x_path = sum(x_axis[index] * path[index] for index in range(3))
        dot_y_path = sum(y_axis[index] * path[index] for index in range(3))
        dot_xy = sum(x_axis[index] * y_axis[index] for index in range(3))

        self.assertAlmostEqual(dot_x_path, 0.0)
        self.assertAlmostEqual(dot_y_path, 0.0)
        self.assertAlmostEqual(dot_xy, 0.0)
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in x_axis)), 1.0)
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in y_axis)), 1.0)

    def test_loft_parser_keeps_legacy_circle_format(self):
        parsed = server._parse_loft_sections("XY:50;30:40")

        self.assertEqual(parsed[0]["shape"], "circle")
        self.assertEqual(parsed[0]["diameter_mm"], 50.0)
        self.assertEqual(parsed[1]["plane"], "30")

    def test_loft_parser_accepts_circle_and_rectangle_sections(self):
        parsed = server._parse_loft_sections(
            "0|circle|50|2|-3;30|rectangle|40|20;60|circle|25"
        )

        self.assertEqual(parsed[0]["center_x_mm"], 2.0)
        self.assertEqual(parsed[0]["center_y_mm"], -3.0)
        self.assertEqual(parsed[1]["shape"], "rectangle")
        self.assertEqual(parsed[1]["width_mm"], 40.0)
        self.assertEqual(parsed[1]["height_mm"], 20.0)

    def test_loft_parser_rejects_non_positive_dimensions(self):
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            server._parse_loft_sections("0|circle|50;30|rectangle|40|0")

    def test_operation_parser_rejects_typographical_errors(self):
        constants = SimpleNamespace(
            kNewBodyOperation=1,
            kJoinOperation=2,
            kCutOperation=3,
            kIntersectOperation=4,
            kSurfaceOperation=5,
        )
        with patch.object(server, "_const", constants):
            self.assertEqual(server._parse_feature_operation("cut"), 3)
            with self.assertRaisesRegex(ValueError, "Unknown operation"):
                server._parse_feature_operation("cuut")


class InventorFeatureComContractTests(unittest.TestCase):
    def test_shell_passes_the_official_both_sides_enum(self):
        calls = []

        class ShellFeatures:
            def CreateShellDefinition(self, *args):
                calls.append(("definition", args))
                return "shell-definition"

            def Add(self, definition):
                calls.append(("add", definition))
                return SimpleNamespace(Name="Shell1")

        component = SimpleNamespace(
            SurfaceBodies=SimpleNamespace(Count=1),
            Features=SimpleNamespace(ShellFeatures=ShellFeatures()),
        )
        document = SimpleNamespace(
            ComponentDefinition=component,
            Activate=lambda: None,
        )
        constants = SimpleNamespace(
            kInsideShellDirection=10,
            kOutsideShellDirection=11,
            kBothSidesShellDirection=12,
        )

        with (
            patch.object(server, "_const", constants),
            patch.object(server, "_get_app", return_value=SimpleNamespace()),
            patch.object(server, "_require_part_document", return_value=document),
        ):
            result = server.shell(2.0, direction="both")

        self.assertEqual(calls[0], ("definition", (None, 0.2, 12)))
        self.assertIn("Shell1", result)

    def test_equal_chamfer_forwards_all_safety_options(self):
        calls = []

        class ChamferFeatures:
            def AddUsingDistance(self, *args):
                calls.append(args)
                return SimpleNamespace(Name="Chamfer1")

        edges = SimpleNamespace(Count=2)
        component = SimpleNamespace(
            Features=SimpleNamespace(ChamferFeatures=ChamferFeatures())
        )
        document = SimpleNamespace(
            ComponentDefinition=component,
            Activate=lambda: None,
        )

        with (
            patch.object(server, "_get_app", return_value=SimpleNamespace()),
            patch.object(server, "_require_part_document", return_value=document),
            patch.object(
                server, "_build_edge_collection", return_value=(edges, "top")
            ),
        ):
            result = server.add_chamfer(
                1.5,
                edges="top",
                automatic_edge_chain=False,
                corner_setback=False,
                preserve_all_features=True,
            )

        self.assertEqual(calls, [(edges, 0.15, False, False, True)])
        self.assertIn("Chamfer1", result)


if __name__ == "__main__":
    unittest.main()
