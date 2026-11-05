// Reuse the locked renderer while bounding all asset paths to one job directory.
import fs from 'node:fs/promises';
import path from 'node:path';
import { Renderer } from '../compositor/src/renderer.mjs';
const [inputFile, outputDir, allowedRoot, mode = 'preview'] = process.argv.slice(2);
if (!['preview', 'publish'].includes(mode)) throw new Error('Unsupported compositor mode');
const input = JSON.parse(await fs.readFile(inputFile, 'utf8'));
class LighthouseRenderer extends Renderer {
  async start() {
    await super.start();
    if (!this.preloadBound) {
      const createPage = this.browser.newPage.bind(this.browser);
      this.browser.newPage = async options => {
        const page = await createPage(options);
        const setContent = page.setContent.bind(page);
        page.setContent = async (html, options) => {
          const result = await setContent(html, options);
          // The core checks both fixed faces, including when the post has no emoji.
          // Load the real bundled fonts; never change its fontsLoaded result or report.
          await page.evaluate(async () => {
            await Promise.all([
              document.fonts.load('40px MVPText', '中文あ'),
              document.fonts.load('38px MVPText', '中文あ'),
              document.fonts.load('40px MVPEmoji', '😀'),
            ]);
          });
          return result;
        };
        return page;
      };
      this.preloadBound = true;
    }
    return this;
  }
}
const renderer = new LighthouseRenderer();
try {
  const result = await renderer.render(input, {baseDir:path.dirname(inputFile), outputDir, mode, allowedRoot});
  await fs.writeFile(path.join(outputDir, 'hub-render-result.json'), JSON.stringify({dir:result.dir,report:result.report,
    adapter:{fontPreloadVersion:'fixed-font-preload-v1'}}, null, 2));
  const failed = result.report.issues.some(x => ['RENDER_FAILED','BROWSER_NOT_INSTALLED'].includes(x.code));
  process.exitCode = failed || !result.report.png ? 1 : 0;
} finally { await renderer.close(); }
