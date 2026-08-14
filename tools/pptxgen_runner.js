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

// Search the installed react-icons packs for real export names matching a concept.
// Backs both the find_icon command and make_icon's "did you mean" suggestion, so the
// builder never has to guess a name that may not exist. `require` is cached, so repeat
// calls just iterate the already-loaded key lists (fast).
const _ICON_PACKS = ['fa6', 'fa', 'md', 'tb', 'hi2', 'bs', 'ai'];
function searchIcons(query, packs, limit) {
    const raw = String(query || '').toLowerCase();
    const compact = raw.replace(/[^a-z0-9]/g, '');
    if (!compact) return [];
    const tokens = raw.split(/[^a-z0-9]+/).filter(t => t.length >= 3);
    const out = [];
    for (const packName of (packs && packs.length ? packs : _ICON_PACKS)) {
        let pack;
        try { pack = require(`react-icons/${packName}`); } catch (e) { continue; }
        for (const name of Object.keys(pack)) {
            if (name[0] !== name[0].toUpperCase()) continue; // icon exports only
            const lname = name.toLowerCase();
            if (lname.includes(compact) || (tokens.length > 0 && tokens.every(t => lname.includes(t)))) {
                out.push({ name, pack: packName });
            }
        }
    }
    out.sort((a, b) => a.name.length - b.name.length || a.name.localeCompare(b.name));
    return out.slice(0, limit || 40);
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

                // Off-canvas check for TEXT elements: clipped text is a guaranteed-critical
                // visual-QA defect that costs a full rebuild pass to fix later — flagging it
                // here lets the builder correct it in the same turn it authored the slide.
                // Text-bearing objects only (full-bleed decorative rects legitimately touch
                // the edges), and only when x/y/w/h are all plain numbers (inches) — percent
                // strings and autosized boxes are skipped rather than guessed at.
                for (const obj of (pptx.slides[i]._slideObjects || [])) {
                    if (!obj.text) continue;
                    const o = obj.options || {};
                    if ([o.x, o.y, o.w, o.h].some(v => typeof v !== 'number')) continue;
                    const EPS = 0.01;  // exact-fit boxes (x=0, w=13.33) are fine
                    if (o.x < -EPS || o.y < -EPS
                            || o.x + o.w > 13.33 + EPS || o.y + o.h > 7.5 + EPS) {
                        const snippet = (Array.isArray(obj.text)
                            ? obj.text.map(t => t.text || '').join(' ')
                            : String(obj.text)).slice(0, 40);
                        warnings.push(`Slide ${i + 1}: text box "${snippet}" at x=${o.x}, y=${o.y}, `
                            + `w=${o.w}, h=${o.h} extends beyond the 13.33x7.5in canvas — it will `
                            + `render clipped. Move or shrink it now.`);
                    }
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
        else if (msg.cmd === 'find_icon') {
            respond({ success: true, query: msg.query, matches: searchIcons(msg.query, msg.packs, msg.limit || 40) });
            return;
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
                // Turn a miss into a one-step self-correction: strip the pack-style prefix
                // (Fa/Md/Hi…) to recover the concept and suggest real names across packs.
                const concept = iconName.replace(/^[A-Z][a-z]/, '');
                const sugg = searchIcons(concept || iconName, null, 12);
                const hint = sugg.length
                    ? sugg.map(m => `${m.name} (icon_pack '${m.pack}')`).join(', ')
                    : Object.keys(pack).filter(k => k[0] === k[0].toUpperCase()).slice(0, 12).join(', ');
                respond({ success: false, error: `Icon '${iconName}' not in pack '${packName}'. Did you mean: ${hint}. Or call find_icon to search by concept.` });
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
