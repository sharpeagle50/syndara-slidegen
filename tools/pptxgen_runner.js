/**
 * PptxGenJS runner — long-lived Node.js subprocess that receives JSON commands
 * over stdin and writes JSON responses to stdout. Keeps the pptx object alive
 * between calls so slides accumulate across multiple "run" commands.
 *
 * Commands:
 *   {"cmd":"init", "pptx_path":"...", "style":{...}}
 *   {"cmd":"run",  "code":"..."}
 *   {"cmd":"slide_count"}
 *   {"cmd":"save"}
 */

const PptxGenJS = require('pptxgenjs');
const readline = require('readline');

let React, ReactDOMServer, sharp;
try {
    React = require('react');
    ReactDOMServer = require('react-dom/server');
    sharp = require('sharp');
} catch (_) {
    // Icon rendering unavailable — dependencies not installed
}

let pptx = null;
let pptxPath = '';
let style = {};
let strippedStyle = {}; // style colors with # stripped — ready for PptxGenJS

// DrawingML preset-geometry (ST_ShapeType) aliases the model sometimes emits
// that are NOT valid enum values. PptxGenJS passes an unknown shape name
// straight through as prst="...", which is invalid OOXML: LibreOffice silently
// drops the shape and PowerPoint shows a "repair" dialog and renders it as a
// broken diagonal line. We remap at shape-creation time — the earliest possible
// point — so the bad token never reaches the file and every render the builder
// and QA see is already correct (no wasted revision passes downstream).
const PRESET_ALIASES = {
    oval: 'ellipse',
    circle: 'ellipse',
    rectangle: 'rect',
    square: 'rect',
    roundedRect: 'roundRect',
    roundRectangle: 'roundRect',
    roundedRectangle: 'roundRect',
};

const fixPreset = (t) =>
    (typeof t === 'string' && PRESET_ALIASES[t]) ? PRESET_ALIASES[t] : t;

// Wrap pptx.addSlide so every slide's addShape/addText normalizes preset names.
function patchPresetAliases(p) {
    const origAddSlide = p.addSlide.bind(p);
    p.addSlide = function (...slideArgs) {
        const slide = origAddSlide(...slideArgs);
        const origAddShape = slide.addShape.bind(slide);
        slide.addShape = (type, opts) => origAddShape(fixPreset(type), opts);
        const origAddText = slide.addText.bind(slide);
        slide.addText = (txt, opts) => {
            if (opts && typeof opts.shape === 'string') {
                opts = { ...opts, shape: fixPreset(opts.shape) };
            }
            return origAddText(txt, opts);
        };
        return slide;
    };
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });

rl.on('line', (line) => {
    let msg;
    try {
        msg = JSON.parse(line);
    } catch (parseErr) {
        respond({ success: false, error: `Invalid JSON: ${parseErr.message}` });
        return;
    }

    try {
        if (msg.cmd === 'init') {
            pptx = new PptxGenJS();
            patchPresetAliases(pptx);
            pptxPath = msg.pptx_path || '';
            style = msg.style || {};

            // Pre-strip # from all hex color values so agent code never
            // has to call c() — strippedStyle.bg is already 'F7F8FC'
            strippedStyle = {};
            for (const [k, v] of Object.entries(style)) {
                strippedStyle[k] = typeof v === 'string' && v.startsWith('#')
                    ? v.slice(1) : v;
            }

            // 16:9 widescreen
            pptx.defineLayout({ name: 'WIDE', width: 13.33, height: 7.5 });
            pptx.layout = 'WIDE';

            // Auto-define slide masters from palette
            const s = strippedStyle;
            const plain = style.plain_backgrounds === true;
            if (s.bg) {
                pptx.defineSlideMaster({
                    title: 'CONTENT',
                    background: { color: s.bg },
                    objects: plain ? [] : [
                        { rect: { x: 0, y: 0, w: 0.18, h: 7.5,
                            fill: { color: s.accent || s.bg } } },
                    ],
                });
                // Dark master for title/conclusion/section slides
                const darkBg = s.title_bg || s.accent || s.text || s.bg;
                const titleDarkObjects = (plain || s.title_bar_hidden)
                    ? []
                    : [{ rect: { x: 0, y: 6.9, w: 13.33, h: 0.6,
                            fill: { color: s.accent2 || s.accent || darkBg } } }];
                pptx.defineSlideMaster({
                    title: 'TITLE_DARK',
                    background: { color: darkBg },
                    objects: titleDarkObjects,
                });
                pptx.defineSlideMaster({
                    title: 'BLANK',
                    background: { color: s.bg },
                });
            }

            respond({ success: true, message: 'initialized',
                masters: ['CONTENT', 'TITLE_DARK', 'BLANK'] });
        }
        else if (msg.cmd === 'run') {
            if (!pptx) {
                respond({ success: false, error: 'Not initialized — send {"cmd":"init"} first.' });
                return;
            }
            const slidesBefore = pptx.slides.length;

            // Execute with pptx, style (raw), s (pre-stripped), c() helper, PptxGenJS
            const fn = new Function('pptx', 'style', 's', 'c', 'PptxGenJS', msg.code);
            const c = (hex) => typeof hex === 'string' ? hex.replace('#', '') : hex;
            fn(pptx, style, strippedStyle, c, PptxGenJS);

            // Word-count check on newly added slides
            const warnings = [];
            for (let i = slidesBefore; i < pptx.slides.length; i++) {
                let wordCount = 0;
                for (const obj of (pptx.slides[i]._slideObjects || [])) {
                    if (obj.text) {
                        const txt = Array.isArray(obj.text)
                            ? obj.text.map(t => t.text || '').join(' ')
                            : String(obj.text);
                        wordCount += txt.split(/\s+/).filter(w => w.length > 0).length;
                    }
                }
                if (wordCount > 25) {
                    warnings.push(`Slide ${i + 1}: ~${wordCount} words on surface (target ≤20). Trim text.`);
                }
            }

            const resp = { success: true, slide_count: pptx.slides.length };
            if (warnings.length > 0) resp.warnings = warnings;
            respond(resp);
        }
        else if (msg.cmd === 'save') {
            if (!pptx) {
                respond({ success: false, error: 'Not initialized — nothing to save.' });
                return;
            }
            pptx.writeFile({ fileName: pptxPath })
                .then(() => {
                    respond({ success: true, slide_count: pptx.slides.length, path: pptxPath });
                    process.exit(0);
                })
                .catch(err => {
                    respond({ success: false, error: err.message || String(err) });
                    process.exit(1);
                });
            return; // Don't process more lines while saving
        }
        else if (msg.cmd === 'snapshot') {
            // Write a temporary copy so LibreOffice can render slides mid-build
            if (!pptx) {
                respond({ success: false, error: 'Not initialized.' });
                return;
            }
            const snapPath = msg.snap_path || pptxPath;
            pptx.writeFile({ fileName: snapPath })
                .then(() => {
                    respond({ success: true, slide_count: pptx.slides.length, path: snapPath });
                })
                .catch(err => {
                    respond({ success: false, error: err.message || String(err) });
                });
            return; // async — wait for writeFile to finish before processing next line
        }
        else if (msg.cmd === 'render_icon') {
            if (!React || !ReactDOMServer || !sharp) {
                respond({ success: false, error: 'Icon rendering unavailable — react, react-dom, or sharp not installed.' });
                return;
            }
            const packName = msg.icon_pack || 'fa';
            const iconName = msg.icon_name;
            const size = msg.size || 256;
            const color = msg.color ? `#${msg.color.replace('#', '')}` : '#000000';
            let pack;
            try {
                pack = require(`react-icons/${packName}`);
            } catch (e) {
                respond({ success: false, error: `Icon pack 'react-icons/${packName}' not found: ${e.message}` });
                return;
            }
            const IconComponent = pack[iconName];
            if (!IconComponent) {
                const available = Object.keys(pack).filter(k => k[0] === k[0].toUpperCase()).slice(0, 20);
                respond({ success: false, error: `Icon '${iconName}' not in pack '${packName}'. Examples: ${available.join(', ')}` });
                return;
            }
            const element = React.createElement(IconComponent, { size, color });
            const svgString = ReactDOMServer.renderToString(element);
            sharp(Buffer.from(svgString))
                .resize(size, size)
                .png()
                .toBuffer()
                .then(buf => {
                    const base64 = buf.toString('base64');
                    const outPath = msg.out_path || '';
                    if (outPath) {
                        const fs = require('fs');
                        fs.writeFileSync(outPath, buf);
                        respond({ success: true, base64, path: outPath, size });
                    } else {
                        respond({ success: true, base64, size });
                    }
                })
                .catch(err => {
                    respond({ success: false, error: `Sharp conversion failed: ${err.message}` });
                });
            return;
        }
        else if (msg.cmd === 'slide_count') {
            respond({ success: true, slide_count: pptx ? pptx.slides.length : 0 });
        }
        else {
            respond({ success: false, error: `Unknown command: ${msg.cmd}` });
        }
    } catch (err) {
        respond({ success: false, error: err.message || String(err) });
    }
});

rl.on('close', () => {
    // If stdin closes without a save, save anyway if we have slides
    if (pptx && pptx.slides.length > 0 && pptxPath) {
        pptx.writeFile({ fileName: pptxPath })
            .catch(() => {})
            .finally(() => process.exit(0));
    } else {
        process.exit(0);
    }
});

function respond(obj) {
    process.stdout.write(JSON.stringify(obj) + '\n');
}
