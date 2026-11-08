"""Make the docs/images screenshots of the debug page in --demo (needs Node + Playwright, dev only).

    python tools/make_screenshots.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qumus_lab.config import load_config  # noqa: E402
from qumus_lab.launcher import build_stack  # noqa: E402
from qumus_lab.service.http import make_server, serve_in_thread  # noqa: E402

JS = r"""
const { chromium } = require('playwright');
(async () => {
  const [base, out] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 });
  await p.goto(base, { waitUntil: 'domcontentloaded' });
  await p.waitForTimeout(2000);
  await p.check('#take');
  await p.waitForTimeout(1200);
  for (const sel of ['#dev-stacking-microscope_camera button[data-action="capture"]',
                     '#dev-exfoliation-desk_camera_left button[data-action="capture"]',
                     '#dev-stacking-desk_camera_right button[data-action="capture"]']) {
    await p.locator(sel).click(); await p.waitForTimeout(400);
  }
  await p.locator('#dev-exfoliation-exfoliation_heater button:has-text("Heat")').first().click();
  await p.waitForTimeout(800);
  await p.locator('#dev-stacking-stamp .jog button:has-text("+")').nth(2).click();
  await p.waitForTimeout(3000);
  await p.screenshot({ path: out + '/debug-page-demo.png' });
  await p.screenshot({ path: out + '/debug-page-demo-full.png', fullPage: true });
  // unknown outcome: the stage vacuum relay was made silent by the python side
  await p.locator('#dev-stacking-stage_vacuum button[data-action="set_switch"]:has-text("On")').click();
  await p.waitForTimeout(2500);
  await p.locator('#dev-stacking-stage_vacuum').scrollIntoViewIfNeeded();
  await p.evaluate(() => window.scrollTo(0, 0));
  await p.screenshot({ path: out + '/debug-page-locked.png' });
  await p.setViewportSize({ width: 390, height: 844 });
  await p.waitForTimeout(800);
  await p.screenshot({ path: out + '/debug-page-phone.png' });
  await b.close();
})();
"""


def main() -> int:
    out = ROOT / "docs" / "images"
    out.mkdir(parents=True, exist_ok=True)
    cfg = load_config(runs_dir=Path(tempfile.mkdtemp()) / "runs")
    stack = build_stack(cfg, demo=True, demo_speed=5.0)
    stack.lab.backends.world.by_device["stage_vacuum"].reply = False  # inject: relay stops answering
    srv = make_server("127.0.0.1", 0, services=stack.services, web=True)
    serve_in_thread(srv)
    js = Path(tempfile.mkdtemp()) / "shot.js"
    js.write_text(JS, encoding="utf-8")
    try:
        subprocess.run(["node", str(js), f"http://127.0.0.1:{srv.server_address[1]}/", str(out)], check=True, timeout=180)
    finally:
        time.sleep(0.2)
        srv.shutdown()
        stack.safe_state("screenshots done")
        stack.close()
    print("wrote", sorted(p.name for p in out.glob("debug-page-*.png")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
