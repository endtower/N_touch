import { ref } from 'vue'

export type MemoryDraftKind = 'character' | 'group' | 'fuzzy_role' | 'experience' | 'kit' | 'observation'
export interface MemoryDraft { id: string; kind: MemoryDraftKind; fields: Record<string, string>; createdAt: string }
export interface DraftField { key: string; label: string; required?: boolean; multiline?: boolean; options?: { value: string; label: string }[] }
export const draftNames: Record<MemoryDraftKind, string> = { character: '角色', group: '群组', fuzzy_role: '模糊角色', experience: '经验', kit: '锦囊', observation: '见闻' }
const storageKey = 'abyss:memory-drafts:v1'

export function draftFields(kind: MemoryDraftKind): DraftField[] {
  switch (kind) {
    case 'character': case 'group': return [
      { key: 'name', label: '名称', required: true },
      { key: 'impression', label: '印象', multiline: true },
      { key: 'session_id', label: '关联会话' },
    ]
    case 'fuzzy_role': return [
      { key: 'name', label: '名称', required: true },
      { key: 'content', label: '内容', required: true, multiline: true },
      { key: 'source_session_id', label: '来源会话', required: true },
    ]
    case 'experience': return [
      { key: 'title', label: '标题', required: true },
      { key: 'summary', label: '摘要', required: true, multiline: true },
      { key: 'category', label: '分类', required: true, options: [{ value: 'speech', label: '话术经验' }, { value: 'decision', label: '决策经验' }] },
    ]
    case 'kit': return [
      { key: 'advice', label: '建议正文', required: true, multiline: true },
      { key: 'dilemma', label: '作用困境', required: true, multiline: true },
      { key: 'category', label: '困境分类', required: true },
    ]
    case 'observation': return [{ key: 'text', label: '见闻正文', required: true, multiline: true }]
  }
}

function readDrafts(): MemoryDraft[] {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(storageKey) || '[]')
    return Array.isArray(stored) ? stored.filter((item): item is MemoryDraft =>
      !!item && typeof item.id === 'string' && item.id.startsWith('draft:') && Object.prototype.hasOwnProperty.call(draftNames, item.kind)
      && typeof item.createdAt === 'string' && item.fields && typeof item.fields === 'object'
      && Object.values(item.fields).every(value => typeof value === 'string')) : []
  } catch { return [] }
}

export const memoryDrafts = ref<MemoryDraft[]>(readDrafts())
export function memoryDraft(id?: string | null) { return memoryDrafts.value.find(item => item.id === id) }
function replaceDrafts(items: MemoryDraft[]) {
  localStorage.setItem(storageKey, JSON.stringify(items))
  memoryDrafts.value = items
}
export function newMemoryDraft(kind: MemoryDraftKind) {
  const item: MemoryDraft = { id: 'draft:' + crypto.randomUUID(), kind, fields: kind === 'experience' ? { category: 'speech' } : {}, createdAt: new Date().toISOString() }
  replaceDrafts([item, ...memoryDrafts.value])
  return item
}
export function updateMemoryDraft(id: string, key: string, value: string) {
  replaceDrafts(memoryDrafts.value.map(item => item.id === id ? { ...item, fields: { ...item.fields, [key]: value } } : item))
}
export function removeMemoryDraft(id: string) { replaceDrafts(memoryDrafts.value.filter(item => item.id !== id)) }
export function draftComplete(item: MemoryDraft) { return draftFields(item.kind).every(field => !field.required || !!item.fields[field.key]?.trim()) }
