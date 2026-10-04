'use strict';
// Observe only the selected page. Never use OS input or change profile preferences.
class NativeDialogs {
  constructor(socket, targetId) {
    this.socket = socket;
    this.targetId = targetId;
    this.sequence = 0;
    this.pending = new Map();
    this.dismissed = [];
    socket.addEventListener('message', event => this.receive(JSON.parse(event.data)));
  }
  send(method, params = {}, sessionId) {
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(new Error(`${method} timed out`)); }, 3000);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  }
  receive(message) {
    const pending = this.pending.get(message.id);
    if (pending) {
      clearTimeout(pending.timer);
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message));
      else pending.resolve(message.result);
    }
    if (message.method === 'FedCm.dialogShown' && message.sessionId === this.sessionId && this.enabled && message.params.dialogType === 'AccountChooser') {
      void this.send('FedCm.dismissDialog', { dialogId: message.params.dialogId }, this.sessionId)
        .then(() => this.dismissed.push('FedCm.AccountChooser'))
        .catch(error => { this.warning = error.message; });
    }
    // Page-owned alert/confirm dialogs block all DOM automation. Accept them
    // only on the selected target and only while overlay dismissal is enabled.
    if (message.method === 'Page.javascriptDialogOpening' && message.sessionId === this.sessionId && this.enabled) {
      void this.send('Page.handleJavaScriptDialog', { accept: true }, this.sessionId)
        .then(() => this.dismissed.push(`Page.${message.params.type || 'dialog'}`))
        .catch(error => { this.warning = error.message; });
    }
  }
  async start(enabled = true) {
    this.enabled = enabled;
    const attached = await this.send('Target.attachToTarget', { targetId: this.targetId, flatten: true });
    this.sessionId = attached.sessionId;
    try { await this.send('FedCm.enable', {}, this.sessionId); }
    catch (error) { this.warning = `Native account chooser monitoring unavailable: ${error.message}`; }
    try { await this.send('Page.enable', {}, this.sessionId); }
    catch (error) { this.warning ||= `Native page-dialog monitoring unavailable: ${error.message}`; }
  }
  async toggle(enabled) {
    this.enabled = enabled;
  }
}
async function watchNativeDialogs(wsUrl, targetId, enabled = true) {
  const socket = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { socket.close(); reject(new Error('Native dialog CDP connection timed out')); }, 3000);
    socket.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    socket.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Native dialog CDP connection failed')); }, { once: true });
  });
  const watcher = new NativeDialogs(socket, targetId);
  try { await watcher.start(enabled); return watcher; }
  catch (error) { socket.close(); throw error; }
}
module.exports = { NativeDialogs, watchNativeDialogs };
