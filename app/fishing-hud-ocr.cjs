// Local OCR for small percentage labels. Images and models stay on this device.
const readline = require('node:readline');
const { createWorker, PSM } = require('./hud-ocr/node_modules/tesseract.js');
const language = require('./hud-ocr/node_modules/@tesseract.js-data/eng');

async function main() {
  const worker = await createWorker('eng', 1, {
    langPath: language.langPath,
    gzip: language.gzip,
    cacheMethod: 'none',
    logger: () => {},
  });
  try {
    await worker.setParameters({
    tessedit_pageseg_mode: PSM.SINGLE_LINE,
    tessedit_char_whitelist: '0123456789%',
    user_defined_dpi: '300',
    });
    process.stdout.write(JSON.stringify({ready:true})+'\n');
    for await (const line of readline.createInterface({input:process.stdin})) {
      const request = JSON.parse(line);
      const images = request.images;
      if (!Array.isArray(images) || images.length > 24) throw new Error('Invalid HUD request');
      await worker.setParameters({tessedit_char_whitelist: request.mode === 'capacity' ? '' : '0123456789%'});
      const readings = [];
      for (const image of images) {
        const {data} = await worker.recognize(Buffer.from(image, 'base64'));
        readings.push({text:data.text.trim(),confidence:data.confidence});
      }
      process.stdout.write(JSON.stringify({readings})+'\n');
    }
  } finally { await worker.terminate(); }
}
main().catch(error => {
  process.stdout.write(JSON.stringify({error:String(error.message)})+'\n');
  process.exitCode = 1;
});
