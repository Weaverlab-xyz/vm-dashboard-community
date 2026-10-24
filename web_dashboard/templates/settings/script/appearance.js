// The Appearance card: the instance's branding (theme, accent or custom colours, logo),
// its contrast checks, and the presets it offers.
// Spread into settingsPage() in settings.html, which includes this file.
function settingsAppearance() {
  return {
    // Every key the card binds is declared here. A field bound with x-model but missing
    // from this object reads as undefined and is dropped from the PATCH body silently —
    // the setting saves, reports success, and changes nothing.
    branding: {
      brand_name: '', brand_full: '', brand_accent: '',
      brand_env_label: '', brand_env_color: '',
      brand_primary: '', brand_secondary: '', brand_accent_hex: '',
    },
    // Swatches come from the server (ui_theme.accent_choices) rather than being repeated
    // here, so the colour shown on the button is the colour the theme will apply.
    brandingAccents: [],
    brandingDefaults: {},
    brandingSaving: false,
    // UI-only: which half of the colour control is showing. Not a config row, hence no
    // brand_ prefix -- everything named brand_* in this object is persisted verbatim.
    brandingCustom: false,
    // The three hexes as a named set, so "match our brand" is one click for the customer
    // this was built for. Lowercase to match what the API stores.
    brandPresets: {
      bt: { label: 'BeyondTrust', primary: '#1903a6', secondary: '#030973', accent: '#ff5400' },
    },
    // The uploaded logo. Deliberately NOT a member of `branding`: that object is PATCHed
    // wholesale when Save is pressed, so a logo in it would carry ~700 KB of base64 along
    // with every colour change. It has its own endpoints and applies immediately.
    brandingLogo: null,       // {url, mime, bytes, width, height, filename} or null
    brandingLogoBusy: false,
    brandingLogoError: '',
    brandingLogoSvgOk: true,

    async loadBranding() {
      if (!this.isAdmin) return;   // loadFeatureFlags sets isAdmin
      try {
        const data = await API.get('/api/setup/branding');
        // Spread over the declared shape rather than replacing it: the server returns the
        // same five keys, but assigning the response wholesale would hand Alpine whatever
        // an older or newer build happens to send.
        this.branding = { ...this.branding, ...(data.config || {}) };
        // The stored trio is the only record that custom mode was chosen; theme_for zeroes
        // brand_accent when it wins, so there is nothing else to read it back from.
        this.brandingCustom = !!this.branding.brand_primary;
        this.brandingAccents = data.accents || [];
        this.brandingDefaults = data.defaults || {};
        // Its own key, not spread into `branding` -- see the note on brandingLogo.
        this.brandingLogo = (data.logo && data.logo.url) ? data.logo : null;
        this.brandingLogoSvgOk = data.logo_svg_allowed !== false;
      } catch (e) {
        // Non-fatal — the card keeps its defaults and the rest of the page still works.
      }
    },

    // Mirrors ui_theme._text_on so the preview above agrees with what the server will
    // render. WCAG relative luminance, same 0.179 split.
    textOn(hex) {
      const m = /^#([0-9a-f]{6})$/i.exec(hex || '');
      if (!m) return '#ffffff';
      const ch = [0, 2, 4].map(i => {
        const c = parseInt(m[1].substr(i, 2), 16) / 255;
        return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
      });
      return (0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]) > 0.179 ? '#0f172a' : '#ffffff';
    },

    // These five mirror ui_theme's _luminance / _ratio / _mix / _step / _ink, for the same
    // reason textOn above is duplicated and says so: the chrome is baked into the page by
    // the server-side context processor, so the only way to show a colour before the
    // reload is to recompute it here -- and a preview that disagreed with the server would
    // be worse than no preview at all. Keep the constants (0.179, 4.5, the 19 5% steps)
    // identical on both sides.
    lumin(hex) {
      const m = /^#([0-9a-f]{6})$/i.exec(hex || '');
      if (!m) return null;
      const ch = [0, 2, 4].map(i => {
        const c = parseInt(m[1].substr(i, 2), 16) / 255;
        return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
      });
      return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2];
    },

    contrastRatio(a, b) {
      const la = this.lumin(a), lb = this.lumin(b);
      if (la === null || lb === null) return 1;
      return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
    },

    mixHex(a, b, t) {
      const pa = /^#([0-9a-f]{6})$/i.exec(a || '');
      const pb = /^#([0-9a-f]{6})$/i.exec(b || '');
      if (!pa || !pb) return a || '#000000';
      return '#' + [0, 2, 4].map(i => {
        const ca = parseInt(pa[1].substr(i, 2), 16);
        const cb = parseInt(pb[1].substr(i, 2), 16);
        return Math.round(ca + (cb - ca) * t).toString(16).padStart(2, '0');
      }).join('');
    },

    // Toward the surface's OWN legible foreground, not a fixed lighten or darken -- see
    // the note on ui_theme._step for why either fixed direction is wrong half the time.
    stepHex(bg, t) { return this.mixHex(bg, this.textOn(bg), t); },

    // A brand colour darkened until it passes AA as text on white. A colour chosen to work
    // as a filled surface is usually too light to read at 16px.
    inkOn(hex) {
      let out = hex;
      for (let i = 0; i < 19; i++) {
        out = this.mixHex(hex, '#000000', i * 0.05);
        if (this.contrastRatio(out, '#ffffff') >= 4.5) return out;
      }
      return out;
    },

    // Selecting a preset and selecting custom are mutually exclusive: ui_theme lets the
    // trio outrank a stored accent, so leaving both set would show a selected swatch that
    // is not what renders.
    pickPreset(key) {
      this.branding.brand_accent = key;
      this.brandingCustom = false;
      this.branding.brand_primary = '';
      this.branding.brand_secondary = '';
      this.branding.brand_accent_hex = '';
    },

    applyBrandPreset(key) {
      const p = this.brandPresets[key];
      if (!p) return;
      this.brandingCustom = true;
      this.branding.brand_accent = '';
      this.branding.brand_primary = p.primary;
      this.branding.brand_secondary = p.secondary;
      this.branding.brand_accent_hex = p.accent;
    },

    // Normalise what a person typed or pasted. Accepts '1903A6' and '#1903a6'; anything
    // else is left alone so a half-typed value is not destroyed mid-keystroke.
    normaliseHex(field) {
      let v = (this.branding[field] || '').trim().toLowerCase();
      if (/^[0-9a-f]{6}$/.test(v)) v = '#' + v;
      if (/^#[0-9a-f]{6}$/.test(v)) this.branding[field] = v;
    },

    // Only the pairs that actually fail. Advisory, never blocking: a customer's brand is
    // what it is, and the job is to make the cost visible before the reload rather than to
    // refuse it. The foregrounds are computed, so these fire only for a genuine mid-tone
    // where neither black nor white clears AA.
    brandingContrastWarnings() {
      const p = this.branding.brand_primary;
      const s = this.branding.brand_secondary;
      const a = this.branding.brand_accent_hex;
      if (!p || !s || !a) return [];
      const out = [];
      const check = (label, bg) => {
        const r = this.contrastRatio(this.textOn(bg), bg);
        if (r < 4.5) out.push(label + ' is ' + r.toFixed(1) + ':1.');
      };
      check('Navigation text on your secondary colour', s);
      check('Button text on your primary colour', p);
      check('Badge text on your accent colour', a);
      return out;
    },

    // The swatch the banner falls back to when no colour is picked. '#1e3a8a' is the
    // shipped blue, used when no accent is chosen either.
    //
    // A custom palette answers first, because theme_for resolves the banner's default to
    // hex.nav_bg and the custom trio sets that to the secondary colour. Reading the accent
    // list here instead would show a blue strip and then render a navy one.
    accentSwatch() {
      if (this.brandingCustom && this.branding.brand_secondary) {
        return this.branding.brand_secondary;
      }
      const a = this.brandingAccents.find(x => x.key === this.branding.brand_accent);
      return a ? a.swatch : '#1e3a8a';
    },

    async saveBranding() {
      this.brandingSaving = true;
      try {
        await API.patch('/api/setup/branding', this.branding);
        this.showToast('Appearance saved.');
        // Reload rather than repaint: the brand and the accent are baked into the page by
        // the server-side context processor, so there is nothing on this page for Alpine
        // to re-render. Delayed past config_service's 5s cache TTL would be worse UX than
        // an immediate reload that occasionally shows the previous colours for a moment.
        setTimeout(() => window.location.reload(), 600);
      } catch (e) {
        this.showToast('Could not save appearance: ' + e.message, 'error');
        this.brandingSaving = false;
      }
    },

    // Applies immediately rather than on Save. One request, one outcome to report -- a
    // deferred binary would mean two writes behind one button and a toast that cannot
    // honestly describe a half-failure.
    async uploadLogo(ev) {
      const file = ev.target.files && ev.target.files[0];
      // Clear the input straight away, or re-picking the SAME file after a rejection does
      // not fire @change and the retry silently does nothing.
      ev.target.value = '';
      if (!file) return;

      this.brandingLogoError = '';
      // A local check for a fast message; the server enforces the real limit.
      if (file.size > 512 * 1024) {
        this.brandingLogoError = 'That file is ' + Math.round(file.size / 1024)
          + ' KB. The limit is 512 KB.';
        return;
      }

      this.brandingLogoBusy = true;
      try {
        const data = await API.post('/api/setup/branding/logo', {
          filename: file.name,
          content_type: file.type || '',
          content_b64: await window.fileToBase64(file),
        });
        this.brandingLogo = data;
        this.showToast('Logo uploaded.');
        // Same reason saveBranding reloads: the mark is rendered by the server-side
        // context processor, so there is nothing here for Alpine to repaint.
        setTimeout(() => window.location.reload(), 600);
      } catch (e) {
        this.brandingLogoError = e.message || 'The upload failed.';
        this.brandingLogoBusy = false;
      }
    },

    async removeLogo() {
      this.brandingLogoBusy = true;
      this.brandingLogoError = '';
      try {
        await API.delete('/api/setup/branding/logo');
        this.brandingLogo = null;
        this.showToast('Logo removed.');
        setTimeout(() => window.location.reload(), 600);
      } catch (e) {
        this.brandingLogoError = e.message || 'Could not remove the logo.';
        this.brandingLogoBusy = false;
      }
    },

    async resetBranding() {
      this.branding = {
        brand_name: '', brand_full: '', brand_accent: '',
        brand_env_label: '', brand_env_color: '',
        brand_primary: '', brand_secondary: '', brand_accent_hex: '',
      };
      this.brandingCustom = false;
      // "Reset to defaults" that left a customer logo in the nav bar would read as a bug.
      // Guarded so a failed delete does not abort the colour reset -- the two are separate
      // writes, and the colours are the ones the button is named after.
      if (this.brandingLogo) {
        try {
          await API.delete('/api/setup/branding/logo');
          this.brandingLogo = null;
        } catch (e) {
          this.brandingLogoError = 'The colours were reset, but the logo could not be '
            + 'removed: ' + (e.message || 'unknown error');
        }
      }
      await this.saveBranding();
    },
  };
}
