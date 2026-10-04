// Runs inside the existing BetterWright sandbox; never starts another browser.
const facebookPages = {
  version: '2',
  config(options) {
    for (const key of ['run_id', 'account', 'page_name']) {
      if (typeof options[key] !== 'string' || !options[key].trim()) throw new Error(`${key} must be a non-empty string`);
    }
    const browseSeconds = options.browse_seconds ?? 120;
    const settleMs = options.post_fill_delay_ms ?? 0;
    const successBrowseSeconds = options.post_success_browse_seconds ?? 30;
    if (!Number.isInteger(browseSeconds) || browseSeconds < 30 || browseSeconds > 180) throw new Error('browse_seconds must be an integer from 30 to 180');
    if (!Number.isInteger(settleMs) || settleMs < 0 || settleMs > 180000) throw new Error('post_fill_delay_ms must be an integer from 0 to 180000');
    if (!Number.isInteger(successBrowseSeconds) || successBrowseSeconds < 0 || successBrowseSeconds > 60) throw new Error('post_success_browse_seconds must be an integer from 0 to 60');
    return options;
  },
  async measure(checkpoint, step, operation) {
    const started = Date.now();
    try { return await operation(); }
    finally {
      checkpoint.timings ||= {};
      checkpoint.timings[step] = (checkpoint.timings[step] || 0) + Date.now() - started;
    }
  },
  async activeAccountId() {
    // Read only the non-secret identity cookie. Never return authentication cookies.
    return page.evaluate(() => {
      if (!/(^|\.)facebook\.com$/.test(location.hostname)) return null;
      return document.cookie.split(';').map(item => item.trim()).find(item => item.startsWith('c_user='))?.slice(7) || null;
    });
  },
  async login(config, checkpoint, timeout = 60000) {
    const accountName = config.account_name || config.account;
    const started = Date.now();
    const deadline = started + timeout;
    let identityObserved = false;
    const input = page.locator('input[name="pass"]').first();
    const card = config.account_selector ? page.locator(config.account_selector)
      : page.getByRole('button').filter({ hasText: accountName });
    // Facebook can raise a page-owned alert while selecting a saved account or
    // submitting its password. Arm the browser dialog handler immediately
    // before each action so the alert cannot wedge the CDP session. Keep this
    // optional for older runners and unit fixtures without a dialogs helper.
    const acceptNextDialog = async () => {
      if (typeof dialogs !== 'undefined' && dialogs && typeof dialogs.acceptNext === 'function') {
        await dialogs.acceptNext();
      }
    };
    while (Date.now() < deadline) {
      const identity = await page.evaluate(expectedName => {
        if (!/(^|\.)facebook\.com$/.test(location.hostname)) return null;
        const id = document.cookie.split(';').map(item => item.trim()).find(item => item.startsWith('c_user='))?.slice(7);
        if (!id) return null;
        const normalize = text => text.replace(/\s+/g, ' ').trim();
        const selfLinks = Array.from(document.querySelectorAll('a[href]')).filter(link => {
          const url = new URL(link.href, location.href);
          return /(^|\.)facebook\.com$/.test(url.hostname) &&
            (url.searchParams.get('id') === id || url.pathname === '/' + id || url.pathname === '/' + id + '/');
        });
        return { id, matched: selfLinks.some(link => normalize(link.innerText || '') === normalize(expectedName)) };
      }, accountName).catch(error => {
        if (/execution context.*destroyed|navigation|cannot find context/i.test(error.message)) return null;
        throw error;
      });
      identityObserved = Boolean(identity);
      if (identity && (identity.matched || config.account_id === identity.id)) {
        checkpoint.accountId = identity.id;
        return 'signed_in';
      }
      // A cookie alone never authorizes creating a Page for an unverified account.
      if (!identity && !checkpoint.loginSubmitted && await input.isVisible().catch(() => false)) {
        if (!config.password) throw new Error('Password prompt appeared but no task password was supplied');
        await input.fill(config.password);
        checkpoint.loginSubmitted = true;
        await acceptNextDialog();
        await input.press('Enter');
      } else if (!identity && !checkpoint.loginCardClicked && await card.isVisible().catch(() => false)) {
        checkpoint.loginCardClicked = true;
        await acceptNextDialog();
        await card.click();
      }
      // Fast bounded polling; identity verification still gates every action.
      await page.waitForTimeout(Math.min(200, Math.max(0, deadline - Date.now())));
    }
    // Report observations, never infer expired sessions from an inert saved card.
    // Keep this non-secret: no page HTML, cookies, or password values in journals.
    checkpoint.loginObservation = {
      elapsedMs: Date.now() - started,
      identityPresent: identityObserved,
      passwordPromptVisible: await input.isVisible().catch(() => false),
      savedCardVisible: await card.isVisible().catch(() => false),
      cardSelectionAttempted: Boolean(checkpoint.loginCardClicked),
      passwordSubmissionAttempted: Boolean(checkpoint.loginSubmitted)
    };
    throw new Error(`Login identity did not become verifiable within ${timeout / 1000} seconds; ${JSON.stringify(checkpoint.loginObservation)}. Inspect once, including browser-owned popups if native dialog monitoring warned. Saved cards do not prove active or expired sessions. Keep this checkpoint and do not seed identity manually.`);
  },
  async assertAccount(checkpoint) {
    if (!checkpoint.accountId || await this.activeAccountId() !== checkpoint.accountId) {
      throw new Error('Signed-in account does not match this checkpoint; inspect identity before continuing');
    }
  },
  async mfaRequired(checkpoint) {
    const url = page.url();
    if (!/\/auth_platform\/codesubmit(?:\/|$)/i.test(url)) return false;
    checkpoint.stage = 'mfa_required';
    checkpoint.mfa = { url };
    return true;
  },
  async browseAfterCreation(checkpoint, seconds) {
    if (!seconds || checkpoint.postCreateBrowsed) return;
    const started = Date.now();
    const deadline = started + seconds * 1000;
    await workflow.navigate('https://www.facebook.com/');
    while (Date.now() < deadline) {
      if (page.mouse?.wheel) await page.mouse.wheel(0, 550).catch(() => {});
      await page.waitForTimeout(Math.min(3000, Math.max(0, deadline - Date.now())));
    }
    checkpoint.postCreateBrowsed = true;
    checkpoint.postCreateBrowsing = { elapsedMs: Date.now() - started, seconds };
  },
  async prepare(options) {
    const config = this.config(options);
    const checkpoint = workflow.begin(config.run_id, config.account);
    if (checkpoint.pageName && checkpoint.pageName !== config.page_name) throw new Error('Page name differs from the saved run; keep the original mapping');
    if (['submitting', 'created', 'logged_out', 'mfa_required'].includes(checkpoint.stage)) return checkpoint;
    if (checkpoint.stage === 'submission_reserved') {
      await this.assertAccount(checkpoint);
      await workflow.validateCreation({ selector: config.create_selector });
      return checkpoint;
    }
    if (checkpoint.accountId) {
      await this.assertAccount(checkpoint);
    } else {
      // Keep the current tab: an existing session or delayed login can finish without reloading.
      if (!/^https:\/\/(?:[^/]+\.)?facebook\.com(?:\/|$)/.test(page.url())) {
        await workflow.navigate('https://www.facebook.com/');
      }
      await this.measure(checkpoint, 'loginMs', async () => {
        try { await this.login(config, checkpoint); }
        catch (error) {
          if (await this.mfaRequired(checkpoint)) return;
          throw error;
        }
        await this.mfaRequired(checkpoint);
      });
      if (checkpoint.stage === 'mfa_required') return checkpoint;
    }
    await workflow.dismissPagePrompts?.();
    await this.measure(checkpoint, 'browsingMs', () => workflow.browseFeed({ seconds: config.browse_seconds ?? 120, discoverVideoSurface: true }));
    if (!checkpoint.browsed) throw new Error('Feed browsing completed without observed video playback; inspect Videos/Reels once before continuing');
    if (!page.url().startsWith('https://www.facebook.com/pages/create')) await this.measure(checkpoint, 'formNavigationMs', () => workflow.navigate('https://www.facebook.com/pages/create/'));
    await this.measure(checkpoint, 'formFillMs', () => workflow.fillPage({
      timeout: 45000,
      pageName: config.page_name,
      bio: config.bio || 'Short-form reels and video content.',
      category: config.category || 'Reel creator',
      nameSelector: config.name_selector,
      categorySelector: config.category_selector,
      optionSelector: config.option_selector,
      bioSelector: config.bio_selector
    }));
    // Keep the completed form visible for a deliberate settling period before
    // reserving the one allowed creation attempt.
    await this.measure(checkpoint, 'postFillDelayMs', () => page.waitForTimeout(config.post_fill_delay_ms ?? 0));
    return workflow.beforeCreate(config.page_name);
  },
  async finish(options) {
    const config = this.config(options);
    const checkpoint = workflow.begin(config.run_id, config.account);
    if (checkpoint.pageName && checkpoint.pageName !== config.page_name) throw new Error('Page name differs from the saved run; keep the original mapping');
    if (checkpoint.stage === 'logged_out' || checkpoint.stage === 'mfa_required') return checkpoint;
    if (checkpoint.stage === 'created' && !await this.activeAccountId()) {
      await page.getByText('Use another profile', { exact: true }).waitFor({ state: 'visible', timeout: 20000 });
      return workflow.loggedOut();
    }
    // Older jobs without an identity checkpoint require explicit identity inspection.
    await this.assertAccount(checkpoint);
    if (checkpoint.stage === 'submitting') {
      await this.measure(checkpoint, 'confirmationMs', () => workflow.confirmCreated(config.confirmation || {}));
    }
    if (checkpoint.stage !== 'created') throw new Error(`Cannot finish stage ${checkpoint.stage}; prepare or inspect without another creation click`);
    await this.measure(checkpoint, 'postCreateBrowsingMs', () => this.browseAfterCreation(checkpoint, config.post_success_browse_seconds ?? 30));
    return this.measure(checkpoint, 'logoutMs', () => workflow.logout(config.logout || {}));
  }
};
