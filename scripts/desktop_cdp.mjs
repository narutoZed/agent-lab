/** Minimal CDP transport for the user's own WorkBuddy desktop window. Node 22+. */
import { setTimeout as delay } from 'node:timers/promises';

export function endpoint(port) {
  const n = Number(port);
  if (!Number.isInteger(n) || n < 1024 || n > 65535) throw Error('port 必须为 1024–65535');
  return `http://127.0.0.1:${n}`;
}

export function validateSocket(url, port) {
  const u = new URL(url);
  if (u.protocol !== 'ws:' || !['127.0.0.1', 'localhost', '[::1]'].includes(u.hostname) || Number(u.port) !== Number(port) || u.username || u.password) {
    throw Error('拒绝连接非本机或不同端口的调试目标');
  }
  return u.href;
}

export async function targets(port, timeout = 3000) {
  const response = await fetch(`${endpoint(port)}/json/list`, { signal: AbortSignal.timeout(timeout), redirect: 'error' });
  if (!response.ok) throw Error(`本地调试端口返回 HTTP ${response.status}`);
  const items = await response.json();
  if (!Array.isArray(items)) throw Error('调试目标列表格式无效');
  return items.filter(t => t.type === 'page' && /\/app\.asar\/renderer\/index\.html(?:[?#]|$)/i.test(t.url || '') && /workbuddy/i.test(t.url || ''));
}

export class CDP {
  constructor(socket) {
    this.socket = socket; this.next = 1; this.pending = new Map();
    socket.addEventListener('message', e => {
      let msg; try { msg = JSON.parse(String(e.data)); } catch { return; }
      const p = this.pending.get(msg.id); if (!p) return;
      this.pending.delete(msg.id); clearTimeout(p.timer);
      msg.error ? p.reject(Error(`CDP: ${msg.error.message}`)) : p.resolve(msg.result);
    });
    socket.addEventListener('close', () => this.failAll('桌面调试连接断开；写操作结果可能不确定，勿自动重发'));
    socket.addEventListener('error', () => this.failAll('桌面调试连接出错'));
  }
  failAll(message) {
    for (const p of this.pending.values()) { clearTimeout(p.timer); p.reject(Error(message)); }
    this.pending.clear();
  }
  static async connect(url, port, timeout = 5000) {
    const ws = new WebSocket(validateSocket(url, port));
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { ws.close(); reject(Error('连接桌面调试端口超时')); }, timeout);
      ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
      ws.addEventListener('error', () => { clearTimeout(timer); reject(Error('无法连接桌面调试端口')); }, { once: true });
    });
    return new CDP(ws);
  }
  call(method, params = {}, timeout = 15000) {
    const id = this.next++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.pending.delete(id); reject(Error(`等待 ${method} 超时；写操作不得自动重发`)); }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      try { this.socket.send(JSON.stringify({ id, method, params })); }
      catch (error) { clearTimeout(timer); this.pending.delete(id); reject(error); }
    });
  }
  async evaluate(expression, timeout = 15000) {
    const data = await this.call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, timeout);
    if (data.exceptionDetails) throw Error(data.exceptionDetails.exception?.description || data.exceptionDetails.text || '桌面接口执行失败');
    return data.result?.value;
  }
  close() { this.socket.close(); this.failAll('连接已关闭'); }
}

export const IDENTITY_EXPRESSION = `(() => ({
  title: document.title,
  version: globalThis.workbuddyDesktop?.appVersion,
  configDir: globalThis.workbuddyDesktop?.configDir,
  platform: globalThis.workbuddyDesktop?.platform,
  focused: document.hasFocus(),
  visibility: document.visibilityState,
  modernBridge: typeof globalThis.__wbInvoke === 'function'
}))()`;

export function selectDesktopWindow(found) {
  if (found.length === 1) return found[0];
  if (!found.length) throw Error('没有找到兼容桌面主窗口；请检查客户端与本机控制端口');
  const profiles = new Set(found.map(x => JSON.stringify([x.info.configDir, x.info.version, x.info.platform])));
  if (profiles.size !== 1 || !found.every(x => x.info.configDir)) {
    throw Error(`找到 ${found.length} 个不同配置的桌面窗口；请使用 --target 指定连接目标`);
  }
  // Same installed profile shares the conversations service. No existing session is mutated;
  // submit always creates a new session with an explicit workspace and request identity.
  return [...found].sort((a,b) => Number(!!b.info.focused)-Number(!!a.info.focused)
    || Number(b.info.visibility==='visible')-Number(a.info.visibility==='visible')
    || a.targetId.localeCompare(b.targetId))[0];
}

export async function connectDesktop(port, targetId) {
  const list = await targets(port);
  const matching = targetId ? list.filter(x => x.id === targetId) : list;
  const found = [];
  for (const target of matching) {
    const client = await CDP.connect(target.webSocketDebuggerUrl, port);
    try {
      const info = await client.evaluate(IDENTITY_EXPRESSION);
      if (info?.modernBridge && info.version) found.push({ client, info, targetId: target.id });
      else client.close();
    } catch { client.close(); }
  }
  let chosen;
  try { chosen = selectDesktopWindow(found); }
  catch (error) { found.forEach(x => x.client.close()); throw error; }
  found.filter(x=>x!==chosen).forEach(x=>x.client.close());
  return {...chosen, compatibleWindowCount:found.length, windowSelection:found.length>1?'same_profile_window':'unique_window'};
}

export function assertCompatible(info) {
  if (!info.modernBridge || !/^5\.8\./.test(info.version || '')) throw Error(`未支持的桌面接口版本 ${info.version || 'unknown'}；需要核对客户端适配，未发起写操作`);
}

const METHODS = new Set(['list', 'get', 'create', 'sendPrompt', 'requests', 'getPendingInfo', 'resolvePending', 'cancel']);
export function invocation(method, args) {
  if (!METHODS.has(method)) throw Error('CLI 不提供该桌面方法');
  // JSON serialization makes task text data, never executable JavaScript.
  return `(async () => {
    const result = await globalThis.__wbInvoke(${JSON.stringify('wb:conversations:'+method)}, {}, ...${JSON.stringify(args)});
    if (result && typeof result === 'object' && (result.errorCode || result.__wbError)) throw new Error(String(result.errorCode || result.code)+': '+String(result.message||''));
    return result;
  })()`;
}

export async function rpc(client, method, args, timeout = 30000) {
  return client.evaluate(invocation(method, args), timeout);
}

export { delay };
