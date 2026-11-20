// Copy buttons, install tabs, nav background, scroll reveal and reduced-motion video.

for (const button of document.querySelectorAll(".copy")) {
  button.addEventListener("click", async () => {
    const box = button.closest("[data-copy]")
    await navigator.clipboard.writeText(box.dataset.copy)
    box.classList.add("is-copied")
    setTimeout(() => box.classList.remove("is-copied"), 1600)
  })
}

const tabs = [...document.querySelectorAll('[role="tab"]')]
function select(tab) {
  for (const t of tabs) {
    const selected = t === tab
    t.setAttribute("aria-selected", selected)
    t.tabIndex = selected ? 0 : -1
    document.getElementById(t.getAttribute("aria-controls")).hidden = !selected
  }
}
for (const tab of tabs) {
  tab.addEventListener("click", () => select(tab))
  tab.addEventListener("keydown", event => {
    const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key]
    if (!step) return
    const next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length]
    select(next)
    next.focus()
  })
}

const nav = document.querySelector("[data-nav]")
const onScroll = () => nav.classList.toggle("is-scrolled", scrollY > 8)
addEventListener("scroll", onScroll, { passive: true })
onScroll()

const reveal = new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (entry.isIntersecting) {
      entry.target.classList.add("is-visible")
      reveal.unobserve(entry.target)
    }
  }
}, { rootMargin: "0px 0px -8% 0px" })
document.querySelectorAll("[data-reveal]").forEach(el => reveal.observe(el))

if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
  for (const video of document.querySelectorAll("video")) {
    video.pause()
    video.controls = true
  }
}
