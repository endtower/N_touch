export async function memoryRequest(path: string, method = 'GET', body?: object) {
  const response = await fetch(path, { method, headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined })
  let data: any
  try { data = await response.json() } catch { throw new Error('服务未返回记忆数据，请确认后端已更新并启动。') }
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`)
  return data
}
