# complete #D1: Pixel dash rebuild, matching the approved mock

- [x] Reskin the dash shell: dark pixel frame, new header and footer, old font and theme toggle removed — touches ccmetrics/dash/static/index.html, tests/test_dash_render.py; done when the suite is green with the pixel tokens in and the font and toggle gone (.clauaid/arch/06-pixel-shell.md)
- [ ] Rebuild the big burn gauge: fuse bar, flame, clock marker, burnt and left and rate figures — touches ccmetrics/dash/static/index.html; done when the gauge draws from live hero fields and the suite is green (.clauaid/arch/07-pixel-hero.md)
- [ ] Rebuild the limits panel: one meter per cap with a plain advice line — touches ccmetrics/dash/static/index.html; done when every cap row draws from live headroom fields and the suite is green (.clauaid/arch/08-pixel-limits.md)
- [ ] Rebuild the savings panel: top three leaks as cards with copyable fixes — touches ccmetrics/dash/static/index.html; done when the cards draw from live findings and the suite is green (.clauaid/arch/09-pixel-recoverable.md)
- [ ] Rebuild the week grid and month strip of usage windows — touches ccmetrics/dash/static/index.html; done when both draw from live window fields and the suite is green (.clauaid/arch/10-pixel-week-month.md)
- [ ] Rebuild the live session strip, the project list and the value chart — touches ccmetrics/dash/static/index.html; done when all three draw from live payloads and the suite is green (.clauaid/arch/11-pixel-live-projects-value.md)
- [ ] Prove it with tests: no demo number survives and every panel reads real data — touches tests/test_dash_render.py, ccmetrics/dash/static/index.html; done when the new story tests pass with the full suite (.clauaid/arch/12-pixel-stories.md)
