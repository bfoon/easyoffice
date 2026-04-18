/**
 * generate_letterhead.js
 *
 * Reads a JSON config from stdin, writes a .docx to the path in config.output.
 *
 * Usage:
 *   echo '<json>' | node generate_letterhead.js
 *
 * Config shape:
 * {
 *   "output":          "/tmp/letterhead_abc.docx",
 *   "template_key":    "classic" | "modern" | "bold" | "minimal" | "split" | "elegant",
 *   "company_name":    "Acme Corporation Ltd",
 *   "tagline":         "Innovation · Excellence · Integrity",
 *   "address":         "14 Independence Drive\nBanjul, The Gambia",
 *   "contact_info":    "+220 123 4567  |  info@acme.gm  |  www.acme.gm",
 *   "color_primary":   "#1D9E75",
 *   "color_secondary": "#2C2C2A",
 *   "font_header":     "Georgia",
 *   "font_body":       "Arial",
 *   "logo_path":       "/abs/path/to/logo.png"   // optional, null if none
 * }
 *
 * Exits 0 on success, 1 on error (error message on stderr).
 */

'use strict';

const fs   = require('fs');
const path = require('path');

const {
  Document, Packer, Paragraph, TextRun, ImageRun,
  Header, Footer,
  AlignmentType, BorderStyle, WidthType, ShadingType,
  TabStopType, TabStopPosition,
  PageNumber, HorizontalPositionRelativeFrom, VerticalPositionRelativeFrom,
  Table, TableRow, TableCell, VerticalAlign,
} = require('docx');

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Strip leading # and return uppercase 6-char hex. */
function stripHash(hex) {
  return (hex || '000000').replace(/^#/, '').toUpperCase();
}

/** Blend a hex colour toward white by `amount` (0–255). */
function lightenHex(hex, amount) {
  const h = stripHash(hex);
  const r = Math.min(255, parseInt(h.slice(0, 2), 16) + amount);
  const g = Math.min(255, parseInt(h.slice(2, 4), 16) + amount);
  const b = Math.min(255, parseInt(h.slice(4, 6), 16) + amount);
  return [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('').toUpperCase();
}

/** Split address string into individual lines. */
function addrLines(addr) {
  return (addr || '').split(/\n/).map(l => l.trim()).filter(Boolean);
}

/** Load logo as Buffer + detect type. Returns null if path is falsy. */
function loadLogo(logoPath) {
  if (!logoPath) return null;
  try {
    const data = fs.readFileSync(logoPath);
    const ext  = path.extname(logoPath).replace('.', '').toLowerCase();
    const type = ext === 'jpg' ? 'jpeg' : (ext || 'png');
    return { data, type };
  } catch (e) {
    process.stderr.write(`Warning: could not read logo at ${logoPath}: ${e.message}\n`);
    return null;
  }
}

// ── A4 page constants (DXA — 1440 DXA = 1 inch) ──────────────────────────────
const PAGE_W  = 11906;   // A4 width  in DXA
const PAGE_H  = 16838;   // A4 height in DXA
const MARGIN  = 1080;    // ~0.75 inch margins
const CONTENT_W = PAGE_W - MARGIN * 2;   // 9746 DXA

// ── Template builders ─────────────────────────────────────────────────────────

/**
 * Each builder returns { headerChildren, footerChildren, bodyPlaceholders }
 * where bodyPlaceholders is an array of Paragraphs to pre-fill the body
 * area so editors know where to type.
 */

// ─── CLASSIC ──────────────────────────────────────────────────────────────────
function buildClassic(cfg, logo) {
  const primary   = stripHash(cfg.color_primary);
  const secondary = stripHash(cfg.color_secondary);
  const fontH = cfg.font_header || 'Georgia';
  const fontB = cfg.font_body   || 'Arial';

  const headerChildren = [];

  // Top accent rule
  headerChildren.push(new Paragraph({
    border: { top: { style: BorderStyle.SINGLE, size: 24, color: primary, space: 0 } },
    spacing: { before: 0, after: 0 },
    children: [],
  }));

  // Logo + company name row (simulate two-column with tab stop)
  const nameRuns = [];
  if (logo) {
    nameRuns.push(new ImageRun({
      type: logo.type,
      data: logo.data,
      transformation: { width: 52, height: 52 },
      altText: { title: 'Logo', description: 'Company logo', name: 'logo' },
    }));
    nameRuns.push(new TextRun({ text: '  ', font: fontB }));
  }
  nameRuns.push(new TextRun({
    text: cfg.company_name || '',
    bold: true,
    size: 36,          // 18pt
    font: fontH,
    color: secondary,
  }));

  headerChildren.push(new Paragraph({
    spacing: { before: 120, after: 40 },
    children: nameRuns,
  }));

  if (cfg.tagline) {
    headerChildren.push(new Paragraph({
      spacing: { before: 0, after: 80 },
      children: [new TextRun({
        text: cfg.tagline,
        italics: true,
        size: 18,
        font: fontB,
        color: primary,
      })],
    }));
  }

  // Address right-aligned (tab stop to right margin)
  const addrLineList = addrLines(cfg.address);
  addrLineList.forEach(line => {
    headerChildren.push(new Paragraph({
      alignment: AlignmentType.RIGHT,
      spacing: { before: 0, after: 0 },
      children: [new TextRun({ text: line, size: 16, font: fontB, color: '888888' })],
    }));
  });
  if (cfg.contact_info) {
    headerChildren.push(new Paragraph({
      alignment: AlignmentType.RIGHT,
      spacing: { before: 0, after: 100 },
      children: [new TextRun({ text: cfg.contact_info, size: 16, font: fontB, color: 'AAAAAA' })],
    }));
  }

  // Divider rule
  headerChildren.push(new Paragraph({
    border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: 'CCCCCC', space: 1 } },
    spacing: { before: 0, after: 0 },
    children: [],
  }));

  // Footer
  const footerChildren = [
    new Paragraph({
      border: { top: { style: BorderStyle.SINGLE, size: 4, color: primary, space: 4 } },
      alignment: AlignmentType.CENTER,
      spacing: { before: 60, after: 40 },
      children: [new TextRun({ text: cfg.contact_info || '', size: 16, font: fontB, color: 'FFFFFF' })],
      shading: { fill: primary, type: ShadingType.CLEAR },
    }),
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { before: 0, after: 60 },
      children: [new TextRun({
        text: addrLines(cfg.address).join('  |  '),
        size: 14, font: fontB, color: 'FFFFFF',
      })],
      shading: { fill: primary, type: ShadingType.CLEAR },
    }),
  ];

  return { headerChildren, footerChildren };
}

// ─── MODERN ───────────────────────────────────────────────────────────────────
function buildModern(cfg, logo) {
  const primary   = stripHash(cfg.color_primary);
  const secondary = stripHash(cfg.color_secondary);
  const fontH = cfg.font_header || 'Georgia';
  const fontB = cfg.font_body   || 'Arial';
  const lightBg = lightenHex(cfg.color_primary, 155);

  const headerChildren = [];

  // Left accent bar effect via shaded paragraph
  const nameRuns = [];
  if (logo) {
    nameRuns.push(new ImageRun({
      type: logo.type, data: logo.data,
      transformation: { width: 48, height: 48 },
      altText: { title: 'Logo', description: 'Company logo', name: 'logo' },
    }));
    nameRuns.push(new TextRun({ text: '  ', font: fontB }));
  }
  nameRuns.push(new TextRun({
    text: cfg.company_name || '',
    bold: true, size: 34, font: fontH, color: secondary,
  }));

  headerChildren.push(new Paragraph({
    border: { left: { style: BorderStyle.SINGLE, size: 24, color: primary, space: 8 } },
    spacing: { before: 100, after: 40 },
    indent: { left: 180 },
    children: nameRuns,
  }));

  if (cfg.tagline) {
    headerChildren.push(new Paragraph({
      indent: { left: 180 },
      spacing: { before: 0, after: 60 },
      children: [new TextRun({ text: cfg.tagline, italics: true, size: 17, font: fontB, color: 'AAAAAA' })],
    }));
  }

  headerChildren.push(new Paragraph({
    alignment: AlignmentType.RIGHT,
    spacing: { before: 0, after: 0 },
    children: [new TextRun({ text: cfg.contact_info || '', size: 16, font: fontB, color: '888888' })],
  }));

  addrLines(cfg.address).forEach(line => {
    headerChildren.push(new Paragraph({
      alignment: AlignmentType.RIGHT,
      spacing: { before: 0, after: 0 },
      children: [new TextRun({ text: line, size: 14, font: fontB, color: 'BBBBBB' })],
    }));
  });

  headerChildren.push(new Paragraph({
    border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: 'E0E0E0', space: 4 } },
    spacing: { before: 80, after: 0 },
    children: [],
  }));

  const footerChildren = [
    new Paragraph({
      border: { top: { style: BorderStyle.SINGLE, size: 6, color: primary, space: 4 } },
      alignment: AlignmentType.CENTER,
      spacing: { before: 60, after: 60 },
      children: [new TextRun({
        text: (cfg.contact_info || '') + '  ·  ' + addrLines(cfg.address).join(' '),
        size: 14, font: fontB, color: primary,
      })],
    }),
  ];

  return { headerChildren, footerChildren };
}

// ─── BOLD ─────────────────────────────────────────────────────────────────────
function buildBold(cfg, logo) {
  const primary   = stripHash(cfg.color_primary);
  const secondary = stripHash(cfg.color_secondary);
  const fontH = cfg.font_header || 'Georgia';
  const fontB = cfg.font_body   || 'Arial';

  const headerChildren = [];

  const nameRuns = [];
  if (logo) {
    nameRuns.push(new ImageRun({
      type: logo.type, data: logo.data,
      transformation: { width: 48, height: 48 },
      altText: { title: 'Logo', description: 'Company logo', name: 'logo' },
    }));
    nameRuns.push(new TextRun({ text: '  ', font: fontB }));
  }
  nameRuns.push(new TextRun({
    text: cfg.company_name || '',
    bold: true, size: 36, font: fontH, color: 'FFFFFF',
  }));

  // Dark banner
  headerChildren.push(new Paragraph({
    spacing: { before: 0, after: 0 },
    shading: { fill: secondary, type: ShadingType.CLEAR },
    children: nameRuns,
  }));

  if (cfg.tagline) {
    headerChildren.push(new Paragraph({
      spacing: { before: 0, after: 0 },
      shading: { fill: secondary, type: ShadingType.CLEAR },
      children: [new TextRun({ text: cfg.tagline, italics: true, size: 18, font: fontB, color: '888888' })],
    }));
  }

  headerChildren.push(new Paragraph({
    spacing: { before: 0, after: 0 },
    shading: { fill: secondary, type: ShadingType.CLEAR },
    children: [new TextRun({
      text: cfg.contact_info || '',
      size: 16, font: fontB, color: 'AAAAAA',
    })],
  }));

  // Primary accent rule under the dark banner
  headerChildren.push(new Paragraph({
    border: { bottom: { style: BorderStyle.SINGLE, size: 12, color: primary, space: 0 } },
    spacing: { before: 0, after: 80 },
    children: [],
  }));

  const footerChildren = [
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { before: 60, after: 60 },
      shading: { fill: secondary, type: ShadingType.CLEAR },
      children: [new TextRun({
        text: cfg.company_name + '  ·  ' + addrLines(cfg.address).join(' ') + '  ·  ' + (cfg.contact_info || ''),
        size: 14, font: fontB, color: '888888',
      })],
    }),
  ];

  return { headerChildren, footerChildren };
}

// ─── MINIMAL ──────────────────────────────────────────────────────────────────
function buildMinimal(cfg, logo) {
  const secondary = stripHash(cfg.color_secondary);
  const fontH = cfg.font_header || 'Georgia';
  const fontB = cfg.font_body   || 'Arial';

  const headerChildren = [];

  const nameRuns = [];
  if (logo) {
    nameRuns.push(new ImageRun({
      type: logo.type, data: logo.data,
      transformation: { width: 44, height: 44 },
      altText: { title: 'Logo', description: 'Company logo', name: 'logo' },
    }));
    nameRuns.push(new TextRun({ text: '  ', font: fontB }));
  }
  nameRuns.push(new TextRun({
    text: cfg.company_name || '',
    bold: true, size: 32, font: fontH, color: secondary,
  }));

  headerChildren.push(new Paragraph({
    spacing: { before: 80, after: 60 },
    children: nameRuns,
  }));

  // Contact + address on one right-aligned line
  const contactLine = [
    addrLines(cfg.address).join(' · '),
    cfg.contact_info || '',
  ].filter(Boolean).join('  ·  ');

  headerChildren.push(new Paragraph({
    alignment: AlignmentType.RIGHT,
    spacing: { before: 0, after: 60 },
    children: [new TextRun({ text: contactLine, size: 15, font: fontB, color: 'BBBBBB' })],
  }));

  headerChildren.push(new Paragraph({
    border: { bottom: { style: BorderStyle.SINGLE, size: 2, color: secondary, space: 2 } },
    spacing: { before: 0, after: 0 },
    children: [],
  }));

  const footerChildren = [
    new Paragraph({
      border: { top: { style: BorderStyle.SINGLE, size: 2, color: 'DDDDDD', space: 4 } },
      alignment: AlignmentType.CENTER,
      spacing: { before: 60, after: 60 },
      children: [new TextRun({
        text: (cfg.contact_info || '') + '   |   ' + addrLines(cfg.address).join(' '),
        size: 14, font: fontB, color: 'BBBBBB',
      })],
    }),
  ];

  return { headerChildren, footerChildren };
}

// ─── SPLIT ────────────────────────────────────────────────────────────────────
function buildSplit(cfg, logo) {
  const primary   = stripHash(cfg.color_primary);
  const secondary = stripHash(cfg.color_secondary);
  const fontH = cfg.font_header || 'Georgia';
  const fontB = cfg.font_body   || 'Arial';

  // Simulate split layout with a two-column table in the header.
  // Left cell: coloured sidebar with logo + name.
  // Right cell: transparent, address right-aligned.
  const leftCell = new TableCell({
    width: { size: 3000, type: WidthType.DXA },
    shading: { fill: primary, type: ShadingType.CLEAR },
    margins: { top: 120, bottom: 120, left: 160, right: 160 },
    borders: {
      top:    { style: BorderStyle.NONE },
      bottom: { style: BorderStyle.NONE },
      left:   { style: BorderStyle.NONE },
      right:  { style: BorderStyle.NONE },
    },
    children: [
      ...(logo ? [new Paragraph({
        spacing: { before: 0, after: 60 },
        children: [new ImageRun({
          type: logo.type, data: logo.data,
          transformation: { width: 60, height: 60 },
          altText: { title: 'Logo', description: 'Company logo', name: 'logo' },
        })],
      })] : []),
      new Paragraph({
        spacing: { before: 0, after: 40 },
        children: [new TextRun({ text: cfg.company_name || '', bold: true, size: 28, font: fontH, color: 'FFFFFF' })],
      }),
      ...(cfg.tagline ? cfg.tagline.split('·').map(t => new Paragraph({
        spacing: { before: 0, after: 20 },
        children: [new TextRun({ text: t.trim(), size: 15, font: fontB, color: 'DDDDDD' })],
      })) : []),
    ],
  });

  const rightCell = new TableCell({
    width: { size: CONTENT_W - 3000, type: WidthType.DXA },
    margins: { top: 120, bottom: 120, left: 240, right: 0 },
    borders: {
      top:    { style: BorderStyle.NONE },
      bottom: { style: BorderStyle.NONE },
      left:   { style: BorderStyle.NONE },
      right:  { style: BorderStyle.NONE },
    },
    verticalAlign: VerticalAlign.BOTTOM,
    children: [
      ...addrLines(cfg.address).map(line => new Paragraph({
        alignment: AlignmentType.RIGHT,
        spacing: { before: 0, after: 20 },
        children: [new TextRun({ text: line, size: 15, font: fontB, color: '888888' })],
      })),
      ...(cfg.contact_info ? [new Paragraph({
        alignment: AlignmentType.RIGHT,
        spacing: { before: 0, after: 0 },
        children: [new TextRun({ text: cfg.contact_info, size: 15, font: fontB, color: 'AAAAAA' })],
      })] : []),
    ],
  });

  const headerTable = new Table({
    width: { size: CONTENT_W, type: WidthType.DXA },
    columnWidths: [3000, CONTENT_W - 3000],
    rows: [new TableRow({ children: [leftCell, rightCell] })],
    borders: {
      top:         { style: BorderStyle.NONE },
      bottom:      { style: BorderStyle.NONE },
      left:        { style: BorderStyle.NONE },
      right:       { style: BorderStyle.NONE },
      insideH:     { style: BorderStyle.NONE },
      insideV:     { style: BorderStyle.NONE },
    },
  });

  const headerChildren = [headerTable];

  const footerChildren = [
    new Paragraph({
      border: { top: { style: BorderStyle.SINGLE, size: 4, color: 'DDDDDD', space: 4 } },
      alignment: AlignmentType.CENTER,
      spacing: { before: 60, after: 60 },
      children: [new TextRun({
        text: (cfg.contact_info || '') + '  ·  ' + addrLines(cfg.address).join(' '),
        size: 14, font: fontB, color: 'AAAAAA',
      })],
    }),
  ];

  return { headerChildren, footerChildren };
}

// ─── ELEGANT ──────────────────────────────────────────────────────────────────
function buildElegant(cfg, logo) {
  const primary   = stripHash(cfg.color_primary);
  const secondary = stripHash(cfg.color_secondary);
  const fontH = cfg.font_header || 'Georgia';
  const fontB = cfg.font_body   || 'Arial';

  const headerChildren = [];

  // Centred logo
  if (logo) {
    headerChildren.push(new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { before: 80, after: 60 },
      children: [new ImageRun({
        type: logo.type, data: logo.data,
        transformation: { width: 64, height: 64 },
        altText: { title: 'Logo', description: 'Company logo', name: 'logo' },
      })],
    }));
  }

  headerChildren.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: logo ? 0 : 80, after: 40 },
    children: [new TextRun({ text: cfg.company_name || '', bold: true, size: 36, font: fontH, color: secondary })],
  }));

  if (cfg.tagline) {
    headerChildren.push(new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { before: 0, after: 80 },
      children: [new TextRun({ text: cfg.tagline, italics: true, size: 18, font: fontB, color: primary })],
    }));
  }

  // Double rule (simulate with two border paragraphs)
  headerChildren.push(new Paragraph({
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: primary, space: 2 } },
    spacing: { before: 40, after: 0 },
    children: [],
  }));
  headerChildren.push(new Paragraph({
    border: { bottom: { style: BorderStyle.SINGLE, size: 2, color: primary, space: 2 } },
    spacing: { before: 0, after: 60 },
    children: [],
  }));

  // Centred contact line
  const infoLine = [
    addrLines(cfg.address).join('  ·  '),
    cfg.contact_info || '',
  ].filter(Boolean).join('  ·  ');

  headerChildren.push(new Paragraph({
    alignment: AlignmentType.CENTER,
    spacing: { before: 0, after: 80 },
    children: [new TextRun({ text: infoLine, size: 15, font: fontB, color: 'AAAAAA' })],
  }));

  const footerChildren = [
    new Paragraph({
      border: { top: { style: BorderStyle.SINGLE, size: 6, color: primary, space: 2 } },
      spacing: { before: 40, after: 0 },
      children: [],
    }),
    new Paragraph({
      border: { top: { style: BorderStyle.SINGLE, size: 2, color: primary, space: 2 } },
      alignment: AlignmentType.CENTER,
      spacing: { before: 60, after: 60 },
      children: [new TextRun({ text: cfg.contact_info || '', size: 15, font: fontB, color: 'AAAAAA' })],
    }),
  ];

  return { headerChildren, footerChildren };
}

// ── Template dispatcher ───────────────────────────────────────────────────────

const BUILDERS = {
  classic: buildClassic,
  modern:  buildModern,
  bold:    buildBold,
  minimal: buildMinimal,
  split:   buildSplit,
  elegant: buildElegant,
};

// ── Body placeholder paragraphs ───────────────────────────────────────────────

function makeBodyPlaceholders(cfg) {
  const fontB = cfg.font_body || 'Arial';
  const today = new Date().toLocaleDateString('en-GB', { day: 'numeric', month: 'long', year: 'numeric' });
  return [
    // Date
    new Paragraph({
      spacing: { before: 240, after: 240 },
      children: [new TextRun({ text: today, size: 22, font: fontB, color: '555555' })],
    }),
    // Recipient block (editable placeholder text)
    new Paragraph({ spacing: { before: 0, after: 80 }, children: [new TextRun({ text: 'Recipient Name', size: 22, font: fontB })] }),
    new Paragraph({ spacing: { before: 0, after: 80 }, children: [new TextRun({ text: 'Recipient Title', size: 22, font: fontB })] }),
    new Paragraph({ spacing: { before: 0, after: 80 }, children: [new TextRun({ text: 'Organisation', size: 22, font: fontB })] }),
    new Paragraph({ spacing: { before: 0, after: 240 }, children: [new TextRun({ text: 'Address', size: 22, font: fontB })] }),
    // Salutation
    new Paragraph({
      spacing: { before: 0, after: 240 },
      children: [new TextRun({ text: 'Dear Sir/Madam,', size: 22, font: fontB })],
    }),
    // Subject line
    new Paragraph({
      spacing: { before: 0, after: 240 },
      children: [new TextRun({ text: 'Re: Subject of Letter', bold: true, size: 22, font: fontB })],
    }),
    // Body paragraphs — empty for user to fill
    new Paragraph({ spacing: { before: 0, after: 240 }, children: [new TextRun({ text: '', size: 22, font: fontB })] }),
    new Paragraph({ spacing: { before: 0, after: 240 }, children: [new TextRun({ text: '', size: 22, font: fontB })] }),
    new Paragraph({ spacing: { before: 0, after: 240 }, children: [new TextRun({ text: '', size: 22, font: fontB })] }),
    // Closing
    new Paragraph({ spacing: { before: 240, after: 80 }, children: [new TextRun({ text: 'Yours sincerely,', size: 22, font: fontB })] }),
    new Paragraph({ spacing: { before: 0, after: 80 }, children: [new TextRun({ text: '', size: 22, font: fontB })] }),
    new Paragraph({ spacing: { before: 0, after: 80 }, children: [new TextRun({ text: '', size: 22, font: fontB })] }),
    new Paragraph({ spacing: { before: 0, after: 80 }, children: [new TextRun({ text: 'Name & Title', bold: true, size: 22, font: fontB })] }),
    new Paragraph({ spacing: { before: 0, after: 80 }, children: [new TextRun({ text: cfg.company_name || '', size: 22, font: fontB })] }),
  ];
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  let raw = '';
  process.stdin.setEncoding('utf8');
  for await (const chunk of process.stdin) raw += chunk;

  let cfg;
  try {
    cfg = JSON.parse(raw);
  } catch (e) {
    process.stderr.write('Error: invalid JSON on stdin\n');
    process.exit(1);
  }

  if (!cfg.output) {
    process.stderr.write('Error: cfg.output is required\n');
    process.exit(1);
  }

  const logo    = loadLogo(cfg.logo_path || null);
  const builder = BUILDERS[cfg.template_key] || buildClassic;
  const { headerChildren, footerChildren } = builder(cfg, logo);
  const bodyChildren = makeBodyPlaceholders(cfg);

  const doc = new Document({
    sections: [{
      properties: {
        page: {
          size:   { width: PAGE_W, height: PAGE_H },
          margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN },
        },
      },
      headers: {
        default: new Header({ children: headerChildren }),
      },
      footers: {
        default: new Footer({ children: footerChildren }),
      },
      children: bodyChildren,
    }],
  });

  const buffer = await Packer.toBuffer(doc);
  fs.writeFileSync(cfg.output, buffer);
  process.stdout.write(JSON.stringify({ ok: true, output: cfg.output }) + '\n');
}

main().catch(err => {
  process.stderr.write(`Fatal: ${err.message}\n${err.stack}\n`);
  process.exit(1);
});
