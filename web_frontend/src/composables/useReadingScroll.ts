import { nextTick, ref, type Ref } from 'vue'

type Position = { top: number; bottom: boolean; key?: string; offset?: number }

// Non-virtual message readers share one rule: preserve the visible card, follow only at the end.
export function useReadingScroll(scroller: Ref<HTMLElement | null>) {
  const following = ref(true)
  function onScroll() {
    const el = scroller.value
    if (el) following.value = el.scrollHeight - el.clientHeight - el.scrollTop <= 64
  }
  function capture(): Position | null {
    const el = scroller.value
    if (!el) return null
    const top = el.getBoundingClientRect().top
    const anchor = Array.from(el.querySelectorAll<HTMLElement>('[data-scroll-key]'))
      .find(item => item.getBoundingClientRect().bottom > top + 1)
    return { top: el.scrollTop, bottom: el.scrollHeight - el.clientHeight - el.scrollTop <= 64,
      key: anchor?.dataset.scrollKey, offset: anchor ? anchor.getBoundingClientRect().top - top : undefined }
  }
  async function restore(position: Position | null, mode: 'preserve' | 'top' | 'bottom' = 'preserve', valid = () => true) {
    await nextTick()
    const el = scroller.value
    if (!el || !valid()) return
    if (mode === 'bottom' || (mode === 'preserve' && position?.bottom)) el.scrollTop = el.scrollHeight
    else if (mode === 'top') el.scrollTop = 0
    else if (position) {
      const anchor = Array.from(el.querySelectorAll<HTMLElement>('[data-scroll-key]')).find(item => item.dataset.scrollKey === position.key)
      if (anchor && position.offset !== undefined)
        el.scrollTop += anchor.getBoundingClientRect().top - el.getBoundingClientRect().top - position.offset
      else el.scrollTop = position.top
    }
    onScroll()
  }
  function latest() { return restore(null, 'bottom') }
  return { following, onScroll, capture, restore, latest }
}
