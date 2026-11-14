"""Generate J3's 34-pin STEP assembly using FreeCAD's Python environment.

Run this file using a Python interpreter with FreeCAD, FreeCADGui, Part and
ImportGui, or via runpy.run_path(path, run_name="__main__") in FreeCAD's console.
For unattended execution, set QT_QPA_PLATFORM=offscreen and a writable
XDG_CACHE_HOME. The script changes no PCB or footprint files.

Source: CHIN-BAN PG1000-01-010P, drawing rev. 1.0 (LCSC C49451494):
https://www.lcsc.com/product-detail/C49451494.html
https://datasheet.lcsc.com/datasheet/pdf/9a106ad967934ef378830dff99ebb5b3.pdf

All dimensions in mm. Nominal, fully extended exterior; internal spring,
plunger tilt and unspecified manufacturing details are omitted. The rounded
tip is represented by a hemisphere. Z=0 is the underside of the seating
flange / PCB top surface. KiCad footprint Y is inverted for model coordinates.
"""

from pathlib import Path
import json
import re

import FreeCAD as App
import FreeCADGui as Gui
import Part


ROOT = Path(__file__).resolve().parents[1]
FOOTPRINT = ROOT / "card-reader-adapter.pretty/pogo-pins-SN-U110.kicad_mod"
OUTPUT = ROOT / "card-reader-adapter.3dshapes"
NAME = "J3_PG1000-01-010P_34"

# Drawing dimensions, relative to the flange seating surface.
TAIL_DIAMETER = 1.0
TAIL_LENGTH = 1.0
FLANGE_DIAMETER = 2.0
FLANGE_HEIGHT = 0.5
BARREL_DIAMETER = 1.5
BARREL_TOP = 5.0
PLUNGER_DIAMETER = 0.9
EXTENDED_HEIGHT = 7.2


def main():
    pads = re.findall(
        r'\(pad "(\d+)" thru_hole circle\s+\(at ([\d.-]+) ([\d.-]+)',
        FOOTPRINT.read_text(),
    )
    pads = sorted((int(n), float(x), -float(y)) for n, x, y in pads)
    assert [n for n, _, _ in pads] == list(range(1, 35)), pads
    assert len({(x, y) for _, x, y in pads}) == 34

    Gui.showMainWindow()
    import ImportGui

    doc = App.newDocument("J3PogoPins")
    doc.Label = "J3 — 34 × CHIN-BAN PG1000-01-010P"
    doc.addObject("App::DocumentObjectGroup", "Pins")
    radius = PLUNGER_DIAMETER / 2
    tip_center = EXTENDED_HEIGHT - radius
    parts = [
        Part.makeCylinder(TAIL_DIAMETER / 2, TAIL_LENGTH,
                          App.Vector(0, 0, -TAIL_LENGTH)),
        Part.makeCylinder(FLANGE_DIAMETER / 2, FLANGE_HEIGHT),
        Part.makeCylinder(BARREL_DIAMETER / 2, BARREL_TOP - FLANGE_HEIGHT,
                          App.Vector(0, 0, FLANGE_HEIGHT)),
        Part.makeCylinder(radius, tip_center - BARREL_TOP,
                          App.Vector(0, 0, BARREL_TOP)),
        Part.makeSphere(radius, App.Vector(0, 0, tip_center),
                        App.Vector(0, 0, 1), 0, 90, 360),
    ]
    pin = parts[0].multiFuse(parts[1:]).removeSplitter()
    assert pin.isValid() and len(pin.Solids) == 1
    assert abs(pin.BoundBox.ZMin + TAIL_LENGTH) < 1e-7
    assert abs(pin.BoundBox.ZMax - EXTENDED_HEIGHT) < 1e-7

    objects = []
    for number, x, y in pads:
        obj = doc.addObject("Part::Feature", f"Pin{number:02d}")
        obj.Label = f"J3.{number} PG1000-01-010P"
        obj.Shape = pin.copy()
        obj.Placement.Base = App.Vector(x, y, 0)
        obj.ViewObject.ShapeColor = (0.83, 0.64, 0.22)
        obj.ViewObject.LineColor = (0.30, 0.22, 0.06)
        obj.addProperty("App::PropertyInteger", "PadNumber", "Source")
        obj.PadNumber = number
        doc.Pins.addObject(obj)
        objects.append(obj)
    doc.recompute()
    OUTPUT.mkdir(exist_ok=True)
    step = OUTPUT / (NAME + ".step")
    ImportGui.export(objects, str(step))
    doc.saveAs(str(OUTPUT / (NAME + ".FCStd")))

    # Re-import the actual deliverable to check solids, positions and height.
    exported = Part.read(str(step))
    assert exported.isValid() and len(exported.Solids) == 34
    expected = sorted((round(x, 6), round(y, 6)) for _, x, y in pads)
    actual = sorted((round(s.BoundBox.Center.x, 6),
                     round(s.BoundBox.Center.y, 6)) for s in exported.Solids)
    assert actual == expected, (actual, expected)
    assert abs(exported.BoundBox.ZMin + TAIL_LENGTH) < 1e-6
    assert abs(exported.BoundBox.ZMax - EXTENDED_HEIGHT) < 1e-6
    print(json.dumps({"step": str(step), "valid_solids": 34,
                      "pad_centers_match": True,
                      "z_range_mm": [-TAIL_LENGTH, EXTENDED_HEIGHT]}))
    App.closeDocument(doc.Name)


if __name__ == "__main__":
    main()
