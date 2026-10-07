/** 数值格式化：null → '—'，数字保留 1 位小数 */
export const fmt = (v: number | null | undefined): string =>
  v == null ? '—' : typeof v === 'number' ? v.toFixed(1) : String(v);

/** 日志着色分类（沿用 Vue 版规则） */
export function logClass(msg: string): '' | 'err' | 'warn' | 'ok' {
  if (msg.includes('错误') || msg.includes('失败') || msg.includes('❌'))
    return 'err';
  if (msg.includes('⚠') || msg.includes('回调') || msg.includes('切换'))
    return 'warn';
  if (msg.includes('已') || msg.includes('OK')) return 'ok';
  return '';
}
