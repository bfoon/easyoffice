/**
 * eo_signature.js — EasyOffice signature engine
 * ─────────────────────────────────────────────
 * Two independent pieces, both dependency-free and safe to load anywhere:
 *
 *   EOSignaturePad    A pressure- and velocity-aware drawing pad. Strokes are
 *                     stored as geometry (not pixels), so the pad can be
 *                     re-rendered on resize, recoloured, and undone without
 *                     any loss of quality. The canvas is never filled, so the
 *                     export is genuinely transparent — not white pixels that
 *                     someone later has to knock out.
 *
 *   EOSignatureImage  Background removal, trimming and typed-signature
 *                     rendering. Mirrors apps/files/signature_image.py so the
 *                     preview a user sees is what the server stores.
 *
 * Why this beats a constant-width pad (the DocuSign default):
 *   • stroke width follows pen pressure on a stylus and drawing speed on a
 *     mouse or finger, which is what makes a signature read as handwriting
 *     rather than as a wire;
 *   • points are joined with quadratic Béziers through their midpoints, so
 *     there are no visible polygon corners at any zoom level;
 *   • strokes taper in and out instead of starting and ending with a blunt cap.
 *
 * Usage:
 *   <script src="{% static 'js/eo_signature.js' %}"></script>
 *
 *   var pad = new EOSignaturePad(document.getElementById('sigCanvas'), {
 *     ink: '#0f172a'
 *   });
 *   pad.isEmpty();
 *   pad.undo();
 *   pad.clear();
 *   pad.toDataURL();                  // trimmed transparent PNG
 *   pad.toBlob(function (blob) {});
 */
(function (global) {
  'use strict';

  /* ═════════════════════════════════════════════════════════════════════════
     Shared canvas helpers
  ═════════════════════════════════════════════════════════════════════════ */

  function makeCanvas(w, h) {
    var c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(w));
    c.height = Math.max(1, Math.round(h));
    return c;
  }

  /** Crop away fully transparent margins, keeping a small padding. */
  function trimCanvas(canvas, paddingPct) {
    var pad = typeof paddingPct === 'number' ? paddingPct : 0.04;
    var ctx = canvas.getContext('2d');
    var w = canvas.width, h = canvas.height;
    if (!w || !h) return canvas;

    var data;
    try {
      data = ctx.getImageData(0, 0, w, h).data;
    } catch (e) {
      return canvas;                      // tainted canvas — leave it alone
    }

    var minX = w, minY = h, maxX = -1, maxY = -1;
    for (var y = 0; y < h; y++) {
      var row = y * w * 4;
      for (var x = 0; x < w; x++) {
        if (data[row + x * 4 + 3] > 8) {
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
        }
      }
    }
    if (maxX < 0) return canvas;          // nothing drawn

    var p = Math.round(Math.max(w, h) * pad);
    minX = Math.max(0, minX - p);
    minY = Math.max(0, minY - p);
    maxX = Math.min(w - 1, maxX + p);
    maxY = Math.min(h - 1, maxY + p);

    var out = makeCanvas(maxX - minX + 1, maxY - minY + 1);
    out.getContext('2d').drawImage(
      canvas, minX, minY, out.width, out.height, 0, 0, out.width, out.height
    );
    return out;
  }

  /** Downscale to a maximum height, preserving the aspect ratio. */
  function limitHeight(canvas, maxHeight) {
    if (!maxHeight || canvas.height <= maxHeight) return canvas;
    var ratio = maxHeight / canvas.height;
    var out = makeCanvas(canvas.width * ratio, maxHeight);
    var c = out.getContext('2d');
    c.imageSmoothingEnabled = true;
    c.imageSmoothingQuality = 'high';
    c.drawImage(canvas, 0, 0, out.width, out.height);
    return out;
  }

  /**
   * Cheap large-radius blur: shrink then grow with smoothing on. Used as a
   * local-illumination estimate, so accuracy matters far less than speed and
   * universal browser support (canvas ctx.filter is still patchy on Safari).
   */
  function blurEstimate(canvas, divisor) {
    var d = divisor || 22;
    var sw = Math.max(1, Math.round(canvas.width / d));
    var sh = Math.max(1, Math.round(canvas.height / d));
    var small = makeCanvas(sw, sh);
    var sc = small.getContext('2d');
    sc.imageSmoothingEnabled = true;
    sc.imageSmoothingQuality = 'high';
    sc.drawImage(canvas, 0, 0, sw, sh);

    var big = makeCanvas(canvas.width, canvas.height);
    var bc = big.getContext('2d');
    bc.imageSmoothingEnabled = true;
    bc.imageSmoothingQuality = 'high';
    bc.drawImage(small, 0, 0, canvas.width, canvas.height);
    return big;
  }

  /** Otsu threshold over a 256-bin histogram. */
  function otsu(hist) {
    var total = 0, sumAll = 0, i;
    for (i = 0; i < 256; i++) { total += hist[i]; sumAll += i * hist[i]; }
    if (!total) return 127;
    var sumBg = 0, wBg = 0, best = -1, bestT = 127;
    for (i = 0; i < 256; i++) {
      wBg += hist[i];
      if (!wBg) continue;
      var wFg = total - wBg;
      if (!wFg) break;
      sumBg += i * hist[i];
      var mBg = sumBg / wBg;
      var mFg = (sumAll - sumBg) / wFg;
      var v = wBg * wFg * (mBg - mFg) * (mBg - mFg);
      if (v > best) { best = v; bestT = i; }
    }
    return bestT;
  }

  function hexToRgb(hex) {
    var m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(String(hex || ''));
    if (!m) return { r: 15, g: 23, b: 42 };
    return {
      r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16)
    };
  }

  /* ═════════════════════════════════════════════════════════════════════════
     EOSignaturePad
  ═════════════════════════════════════════════════════════════════════════ */

  function EOSignaturePad(canvas, options) {
    if (!canvas) throw new Error('EOSignaturePad: canvas is required');
    var o = options || {};

    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');

    this.ink = o.ink || '#0f172a';
    this.minWidth = o.minWidth || 1.1;
    this.maxWidth = o.maxWidth || 4.2;
    this.velocityWeight = o.velocityWeight || 0.75;   // stroke smoothing
    this.velocityScale = o.velocityScale || 2.1;      // speed → thinning
    this.maxUndo = o.maxUndo || 40;
    this.onChange = o.onChange || function () {};

    this.strokes = [];
    this._current = null;
    this._drawing = false;

    this._bindEvents();
    this.resize();

    var self = this;
    if (global.ResizeObserver) {
      this._ro = new ResizeObserver(function () { self.resize(); });
      this._ro.observe(canvas);
    } else {
      global.addEventListener('resize', function () { self.resize(); });
    }
  }

  EOSignaturePad.prototype._bindEvents = function () {
    var self = this;
    var c = this.canvas;
    c.style.touchAction = 'none';

    function point(e) {
      var r = c.getBoundingClientRect();
      var pressure = 0;
      if (e.pointerType === 'pen' && typeof e.pressure === 'number' && e.pressure > 0) {
        pressure = e.pressure;
      }
      return {
        x: (e.clientX - r.left) * (c.width / r.width) / (self._backing || 1),
        y: (e.clientY - r.top) * (c.height / r.height) / (self._backing || 1),
        p: pressure,
        t: (e.timeStamp || Date.now())
      };
    }

    function start(e) {
      if (e.button !== undefined && e.button !== 0 && e.pointerType === 'mouse') return;
      e.preventDefault();
      if (c.setPointerCapture && e.pointerId !== undefined) {
        try { c.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      }
      self._drawing = true;
      self._current = { color: self.ink, points: [point(e)] };
      self.strokes.push(self._current);
      if (self.strokes.length > self.maxUndo) self.strokes.shift();
    }

    function move(e) {
      if (!self._drawing || !self._current) return;
      e.preventDefault();
      var events = (e.getCoalescedEvents && e.getCoalescedEvents()) || [e];
      for (var i = 0; i < events.length; i++) {
        var p = point(events[i]);
        var last = self._current.points[self._current.points.length - 1];
        // Ignore sub-pixel jitter: it adds noise to the velocity estimate.
        if (last && Math.abs(p.x - last.x) < 0.35 && Math.abs(p.y - last.y) < 0.35) continue;
        self._current.points.push(p);
      }
      // Draw only the new geometry. A full re-render on every move would be
      // O(n²) and starts dropping frames on a long signature; the complete
      // re-render happens on pointerup, undo, resize and recolour.
      self._drawPending(self._current);
    }

    function end(e) {
      if (!self._drawing) return;
      self._drawing = false;
      if (self._current && self._current.points.length === 1) {
        // A tap: keep it as a dot rather than discarding the gesture.
        var p0 = self._current.points[0];
        self._current.points.push({ x: p0.x + 0.6, y: p0.y + 0.6, p: p0.p, t: p0.t + 16 });
      }
      self._current = null;
      self.render();
      self.onChange(self);
    }

    if (global.PointerEvent) {
      c.addEventListener('pointerdown', start);
      c.addEventListener('pointermove', move);
      c.addEventListener('pointerup', end);
      c.addEventListener('pointercancel', end);
      c.addEventListener('pointerleave', function (e) { if (self._drawing) end(e); });
    } else {
      // Legacy fallback — normalise mouse/touch into the same shape.
      var wrap = function (fn) {
        return function (ev) {
          var t = ev.touches ? ev.touches[0] : ev;
          if (!t) return fn(ev);
          fn({
            clientX: t.clientX, clientY: t.clientY, pointerType: 'touch',
            timeStamp: ev.timeStamp, preventDefault: function () { ev.preventDefault(); }
          });
        };
      };
      c.addEventListener('mousedown', wrap(start));
      c.addEventListener('mousemove', wrap(move));
      global.addEventListener('mouseup', wrap(end));
      c.addEventListener('touchstart', wrap(start), { passive: false });
      c.addEventListener('touchmove', wrap(move), { passive: false });
      global.addEventListener('touchend', wrap(end));
    }
  };

  /** Re-fit the backing store to the CSS box at device resolution. */
  EOSignaturePad.prototype.resize = function () {
    var dpr = Math.min(global.devicePixelRatio || 1, 3);
    var rect = this.canvas.getBoundingClientRect();
    var w = Math.max(1, rect.width || this.canvas.offsetWidth || 600);
    var h = Math.max(1, rect.height || this.canvas.offsetHeight || 200);

    // Render at 2× the CSS box on top of the DPR: the export is later
    // downscaled, which is what removes the last of the aliasing.
    var backing = dpr * 2;
    this._dpr = dpr;
    this._backing = backing;
    if (this.canvas.width === Math.round(w * backing) &&
        this.canvas.height === Math.round(h * backing)) {
      return;
    }
    this.canvas.width = Math.round(w * backing);
    this.canvas.height = Math.round(h * backing);
    this.render();
  };

  EOSignaturePad.prototype._applyTransform = function () {
    var s = this._backing || 1;
    this.ctx.setTransform(s, 0, 0, s, 0, 0);
    this.ctx.lineCap = 'round';
    this.ctx.lineJoin = 'round';
  };

  EOSignaturePad.prototype.render = function () {
    var ctx = this.ctx;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    this._applyTransform();

    for (var i = 0; i < this.strokes.length; i++) {
      this.strokes[i]._drawn = 0;
      this._renderStroke(this.strokes[i]);
    }
  };

  /** Draw only the segments added since the last pass — no clearing. */
  EOSignaturePad.prototype._drawPending = function (stroke) {
    var pts = stroke.points;
    if (!pts || pts.length < 3) return;

    this._applyTransform();
    this.ctx.fillStyle = stroke.color;

    var widths = this._strokeWidths(pts);
    var start = Math.max(1, stroke._drawn || 0);

    if (!stroke._drawn) {
      // Tapered lead-in, drawn once.
      var firstMid = midpoint(pts[0], pts[1]);
      this._quad(pts[0], firstMid, firstMid, widths[0] * 0.55, widths[0]);
    }

    for (var i = start; i < pts.length - 1; i++) {
      this._quad(midpoint(pts[i - 1], pts[i]), pts[i], midpoint(pts[i], pts[i + 1]),
                 widths[i - 1], widths[i + 1]);
    }
    stroke._drawn = Math.max(1, pts.length - 1);
  };

  EOSignaturePad.prototype._renderStroke = function (stroke) {
    var pts = stroke.points;
    if (!pts || pts.length < 2) return;

    var ctx = this.ctx;
    ctx.fillStyle = stroke.color;

    var widths = this._strokeWidths(pts);
    var i, mid1, mid2;
    stroke._drawn = Math.max(1, pts.length - 1);

    for (i = 1; i < pts.length - 1; i++) {
      mid1 = midpoint(pts[i - 1], pts[i]);
      mid2 = midpoint(pts[i], pts[i + 1]);
      this._quad(mid1, pts[i], mid2, widths[i - 1], widths[i + 1]);
    }
    // First and last segments: straight, tapered.
    this._quad(pts[0], midpoint(pts[0], pts[1]), midpoint(pts[0], pts[1]),
               widths[0] * 0.55, widths[0]);
    var n = pts.length - 1;
    var lastMid = midpoint(pts[n - 1], pts[n]);
    this._quad(lastMid, pts[n], pts[n], widths[n], widths[n] * 0.45);
  };

  /**
   * Per-point stroke width. Pen pressure wins when the hardware reports it;
   * otherwise width is inversely proportional to drawing speed, low-pass
   * filtered so a jittery mouse does not produce a lumpy line.
   */
  EOSignaturePad.prototype._strokeWidths = function (pts) {
    var widths = new Array(pts.length);
    var range = this.maxWidth - this.minWidth;
    var prev = this.maxWidth * 0.8;

    for (var i = 0; i < pts.length; i++) {
      var w;
      if (pts[i].p > 0) {
        w = this.minWidth + range * Math.min(1, pts[i].p * 1.25);
      } else if (i === 0) {
        w = this.maxWidth * 0.8;
      } else {
        var dx = pts[i].x - pts[i - 1].x;
        var dy = pts[i].y - pts[i - 1].y;
        var dt = Math.max(8, pts[i].t - pts[i - 1].t);
        var v = Math.sqrt(dx * dx + dy * dy) / dt * 100;   // px per 100ms
        w = this.maxWidth - Math.min(1, v / (this.velocityScale * 100)) * range;
      }
      w = prev * this.velocityWeight + w * (1 - this.velocityWeight);
      widths[i] = Math.max(this.minWidth, Math.min(this.maxWidth, w));
      prev = widths[i];
    }
    return widths;
  };

  /** Draw one quadratic segment as a run of interpolated-radius dots. */
  EOSignaturePad.prototype._quad = function (from, control, to, w1, w2) {
    var ctx = this.ctx;
    var dist = Math.sqrt(
      Math.pow(to.x - from.x, 2) + Math.pow(to.y - from.y, 2)
    ) + Math.sqrt(
      Math.pow(control.x - from.x, 2) + Math.pow(control.y - from.y, 2)
    );
    var steps = Math.max(2, Math.min(90, Math.ceil(dist * 1.6)));

    for (var i = 0; i <= steps; i++) {
      var t = i / steps;
      var it = 1 - t;
      var x = it * it * from.x + 2 * it * t * control.x + t * t * to.x;
      var y = it * it * from.y + 2 * it * t * control.y + t * t * to.y;
      var r = (w1 + (w2 - w1) * t) / 2;
      ctx.beginPath();
      ctx.arc(x, y, Math.max(0.35, r), 0, Math.PI * 2);
      ctx.fill();
    }
  };

  function midpoint(a, b) {
    return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2, p: (a.p + b.p) / 2, t: b.t };
  }

  EOSignaturePad.prototype.isEmpty = function () {
    for (var i = 0; i < this.strokes.length; i++) {
      if (this.strokes[i].points.length > 1) return false;
    }
    return true;
  };

  EOSignaturePad.prototype.clear = function () {
    this.strokes = [];
    this._current = null;
    this.render();
    this.onChange(this);
  };

  EOSignaturePad.prototype.undo = function () {
    this.strokes.pop();
    this.render();
    this.onChange(this);
  };

  EOSignaturePad.prototype.setInk = function (colour) {
    this.ink = colour;
    for (var i = 0; i < this.strokes.length; i++) this.strokes[i].color = colour;
    this.render();
  };

  EOSignaturePad.prototype.setWeight = function (maxWidth) {
    this.maxWidth = Math.max(1.4, maxWidth);
    this.minWidth = Math.max(0.6, maxWidth * 0.26);
    this.render();
  };

  /** Trimmed, transparent, downscaled export. */
  EOSignaturePad.prototype.toCanvas = function (opts) {
    var o = opts || {};
    var out = trimCanvas(this.canvas, o.padding === undefined ? 0.03 : o.padding);
    return limitHeight(out, o.maxHeight || 420);
  };

  EOSignaturePad.prototype.toDataURL = function (opts) {
    if (this.isEmpty()) return '';
    return this.toCanvas(opts).toDataURL('image/png');
  };

  EOSignaturePad.prototype.toBlob = function (cb, opts) {
    if (this.isEmpty()) { cb(null); return; }
    this.toCanvas(opts).toBlob(cb, 'image/png');
  };

  /* ═════════════════════════════════════════════════════════════════════════
     EOSignatureImage — background removal & typed rendering
  ═════════════════════════════════════════════════════════════════════════ */

  var EOSignatureImage = {

    INK: {
      ink: '#0f172a',
      black: '#111827',
      navy: '#0c235c',
      blue: '#0e57c2',
      gray: '#374151'
    },

    /**
     * Knock the paper out of a photographed or scanned signature.
     *
     * The background is estimated locally (a blurred copy of the image), so a
     * lamp shadow across the page, cream paper and grey newsprint all work.
     * The cut is soft around the automatically chosen threshold, which keeps
     * anti-aliased stroke edges instead of producing a jagged halo.
     *
     * @param {HTMLImageElement|HTMLCanvasElement} source
     * @param {Object}  opts
     * @param {number}  opts.sensitivity  0.5 strict … 1.8 keeps faint pencil
     * @param {string}  opts.ink          hex colour, or null to keep original
     * @param {boolean} opts.despeckle
     * @returns {HTMLCanvasElement}
     */
    clean: function (source, opts) {
      var o = opts || {};
      var sensitivity = o.sensitivity || 1;
      var maxSide = o.maxSide || 1600;

      var sw = source.naturalWidth || source.width;
      var sh = source.naturalHeight || source.height;
      if (!sw || !sh) return makeCanvas(1, 1);

      var scale = Math.min(1, maxSide / Math.max(sw, sh));
      var w = Math.max(1, Math.round(sw * scale));
      var h = Math.max(1, Math.round(sh * scale));

      var base = makeCanvas(w, h);
      var bctx = base.getContext('2d', { willReadFrequently: true });
      bctx.drawImage(source, 0, 0, w, h);

      var img = bctx.getImageData(0, 0, w, h);
      var px = img.data;

      // If the source already carries real transparency (a pad export, a
      // proper cut-out), trust it and skip the knockout entirely.
      var transparentPixels = 0;
      for (var a = 3; a < px.length; a += 4 * 97) {
        if (px[a] < 250) transparentPixels++;
      }
      if (transparentPixels > (px.length / (4 * 97)) * 0.05) {
        return this._finish(base, o);
      }

      // Local background estimate.
      var blurred = blurEstimate(base, 20);
      var bpx = blurred.getContext('2d', { willReadFrequently: true })
                       .getImageData(0, 0, w, h).data;

      // Distance = how much darker than the local paper this pixel is.
      var dist = new Uint8ClampedArray(w * h);
      var hist = new Uint32Array(256);
      var i, j;
      for (i = 0, j = 0; i < px.length; i += 4, j++) {
        var lum = (px[i] * 299 + px[i + 1] * 587 + px[i + 2] * 114) / 1000;
        var blum = (bpx[i] * 299 + bpx[i + 1] * 587 + bpx[i + 2] * 114) / 1000;
        var d = blum - lum;
        if (d < 0) d = 0;
        if (d > 255) d = 255;
        dist[j] = d;
        hist[d | 0]++;
      }

      // Auto threshold, widened into a soft ramp.
      var t = otsu(hist);
      t = Math.max(6, Math.min(200, t / Math.max(0.35, sensitivity)));
      var ramp = Math.max(4, t * 0.45);
      var lo = Math.max(0, t - ramp), hi = Math.min(255, t + ramp);
      var span = Math.max(1, hi - lo);

      var alpha = new Uint8ClampedArray(w * h);
      for (j = 0; j < dist.length; j++) {
        var v = dist[j];
        alpha[j] = v <= lo ? 0 : v >= hi ? 255 : ((v - lo) / span) * 255;
      }

      if (o.despeckle !== false) alpha = this._despeckle(alpha, w, h);

      // ink: null means "keep the pen colour". That is not the same as
      // keeping each pixel's own colour — half-covered edge pixels are part
      // paper, and reusing them leaves a pale fringe once the signature is
      // composited onto a page. Measure the pen colour from the solid core
      // and paint the whole stroke in it.
      var ink;
      if (o.ink === null) {
        var sr = 0, sg = 0, sb = 0, sn = 0;
        for (i = 0, j = 0; i < px.length; i += 4, j++) {
          if (alpha[j] > 200) { sr += px[i]; sg += px[i + 1]; sb += px[i + 2]; sn++; }
        }
        ink = sn
          ? { r: (sr / sn) * 0.86 | 0, g: (sg / sn) * 0.86 | 0, b: (sb / sn) * 0.86 | 0 }
          : hexToRgb(this.INK.ink);
      } else {
        ink = hexToRgb(o.ink || this.INK.ink);
      }

      for (i = 0, j = 0; i < px.length; i += 4, j++) {
        px[i + 3] = alpha[j];
        if (alpha[j]) {
          px[i] = ink.r; px[i + 1] = ink.g; px[i + 2] = ink.b;
        }
      }
      bctx.putImageData(img, 0, 0);
      return this._finish(base, o);
    },

    _finish: function (canvas, o) {
      var out = trimCanvas(canvas, o.padding === undefined ? 0.04 : o.padding);
      return limitHeight(out, o.maxHeight || 500);
    },

    /** Remove dust: pixels with almost no inked neighbours. */
    _despeckle: function (alpha, w, h) {
      var out = new Uint8ClampedArray(alpha.length);
      for (var y = 0; y < h; y++) {
        for (var x = 0; x < w; x++) {
          var idx = y * w + x;
          if (!alpha[idx]) continue;
          var sum = 0, n = 0;
          for (var dy = -1; dy <= 1; dy++) {
            var yy = y + dy;
            if (yy < 0 || yy >= h) continue;
            for (var dx = -1; dx <= 1; dx++) {
              var xx = x + dx;
              if (xx < 0 || xx >= w) continue;
              sum += alpha[yy * w + xx];
              n++;
            }
          }
          out[idx] = (sum / Math.max(1, n)) < 30 ? 0 : alpha[idx];
        }
      }
      return out;
    },

    /** File input → cleaned transparent PNG. Resolves with {dataUrl, canvas}. */
    fromFile: function (file, opts) {
      var self = this;
      return new Promise(function (resolve, reject) {
        if (!file) { reject(new Error('No file')); return; }
        var reader = new FileReader();
        reader.onerror = function () { reject(new Error('Could not read file')); };
        reader.onload = function (e) {
          var img = new Image();
          img.onerror = function () { reject(new Error('Not a readable image')); };
          img.onload = function () {
            var canvas = self.clean(img, opts);
            resolve({
              dataUrl: canvas.toDataURL('image/png'),
              canvas: canvas,
              original: img
            });
          };
          img.src = e.target.result;
        };
        reader.readAsDataURL(file);
      });
    },

    /** Re-clean an existing image element with new settings. */
    fromImage: function (img, opts) {
      var canvas = this.clean(img, opts);
      return { dataUrl: canvas.toDataURL('image/png'), canvas: canvas };
    },

    /**
     * Render typed text to a transparent PNG. Only used for previews and for
     * the "flatten to image" option — the canonical stored value stays
     * `font:Name|Text`, which the server re-renders with the real TTF.
     */
    renderTyped: function (text, font, opts) {
      var o = opts || {};
      var colour = o.ink || this.INK.ink;
      var size = o.size || 220;
      var canvas = makeCanvas(2400, Math.round(size * 2.1));
      var ctx = canvas.getContext('2d');
      ctx.font = '600 ' + size + 'px "' + font + '", cursive';
      ctx.fillStyle = colour;
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'left';
      ctx.fillText(String(text || ''), 40, canvas.height / 2);
      return limitHeight(trimCanvas(canvas, 0.05), o.maxHeight || 420);
    },

    trim: trimCanvas,
    limitHeight: limitHeight
  };

  global.EOSignaturePad = EOSignaturePad;
  global.EOSignatureImage = EOSignatureImage;

})(window);
