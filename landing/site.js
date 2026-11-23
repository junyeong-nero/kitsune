// Without JavaScript the demo rests on its request state, as written in the HTML.

const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches

// Hero demo: background tabs open, then the agent asks for your notes tab.
const demo = document.querySelector("[data-demo]")
if (demo) {
  const $ = selector => demo.querySelector(selector)
  const lines = Object.fromEntries([...demo.querySelectorAll("[data-line]")].map(el => [el.dataset.line, el]))
  const tabs = [...demo.querySelectorAll("[data-tab]")]
  const popup = $(".popup")
  const ext = $(".ext")
  const badge = $(".ext-badge")
  const count = $("[data-count]")
  const request = $(".pp-request")
  const handed = $(".pp-handed")
  const closeTabs = $('[data-act="close-tabs"]')
  const table = $(".notes-table")
  const empty = $(".notes-empty")
  const replay = $('[data-act="replay"]')
  const termBody = $(".term-body")

  const show = (el, on = true) => { el.hidden = !on; if (on && el.closest(".term-body")) termBody.scrollTop = termBody.scrollHeight }
  const setPopup = open => { popup.hidden = !open; ext.setAttribute("aria-expanded", String(open)) }
  const setCount = n => { count.textContent = n; closeTabs.disabled = n === 0 }

  function reset() {
    Object.values(lines).forEach(line => show(line, false))
    tabs.forEach(tab => { show(tab, false); tab.classList.add("is-loading") })
    setPopup(false)
    ;[badge, request, handed, table, replay].forEach(el => show(el, false))
    show(empty)
    setCount(0)
  }

  let run = 0
  async function play() {
    const id = ++run
    reset()
    const step = (ms, fn) => new Promise((resolve, reject) =>
      setTimeout(() => (id === run ? (fn(), resolve()) : reject()), reduced ? 0 : ms))
    try {
      await step(700, () => show(lines.tabs))
      for (const [i, tab] of tabs.entries()) await step(240, () => { show(tab); setCount(i + 1) })
      await step(700, () => show(lines.read))
      await step(900, () => tabs.forEach(tab => tab.classList.remove("is-loading")))
      await step(600, () => { show(lines.request); show(badge) })
      await step(500, () => { show(request); setPopup(true); show(lines.wait) })
    } catch { /* a newer run took over */ }
  }

  const actions = {
    popup: () => setPopup(popup.hidden),
    approve() {
      run++
      show(request, false); show(badge, false); show(lines.wait, false)
      show(handed); show(lines.approved)
      show(empty, false); show(table)
    },
    decline() {
      run++
      show(request, false); show(badge, false); show(lines.wait, false)
      show(lines.declined)
    },
    "take-back"() {
      show(handed, false); show(lines.returned)
    },
    "close-tabs"() {
      run++
      tabs.forEach(tab => show(tab, false)); setCount(0)
      show(lines.closed); show(replay)
    },
    replay: play,
  }
  demo.addEventListener("click", event => {
    const button = event.target.closest("[data-act]")
    if (button && actions[button.dataset.act]) actions[button.dataset.act]()
  })
  play()
}

for (const button of document.querySelectorAll('[data-act="copy"]')) {
  const label = button.querySelector("span")
  const text = label.textContent
  button.addEventListener("click", async () => {
    await navigator.clipboard.writeText(button.parentElement.querySelector("[data-copy-source]").textContent)
    button.classList.add("is-done")
    label.textContent = "Copied"
    setTimeout(() => { button.classList.remove("is-done"); label.textContent = text }, 1600)
  })
}

// The recording loops; it can be paused, and it starts paused under reduced motion.
for (const button of document.querySelectorAll('[data-act="video"]')) {
  const video = button.parentElement.querySelector("video")
  const sync = () => {
    button.textContent = video.paused ? "Play recording" : "Pause recording"
    button.setAttribute("aria-pressed", String(video.paused))
  }
  if (reduced) video.pause()
  button.addEventListener("click", () => (video.paused ? video.play() : video.pause()))
  video.addEventListener("play", sync)
  video.addEventListener("pause", sync)
  sync()
}
