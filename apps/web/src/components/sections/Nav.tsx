import { REPO_URL } from "@/lib/team";

const LINKS = [
  { href: "#how", label: "How we work" },
  { href: "#team", label: "Team" },
  { href: "#roadmap", label: "Roadmap" },
];

export function Nav() {
  return (
    <header className="fixed inset-x-0 top-0 z-40 border-b border-white/5 bg-ink-950/60 backdrop-blur-xl">
      <nav aria-label="Main" className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6">
        <a href="#top" className="flex items-center gap-2.5 font-display text-lg font-semibold">
          <LogoMark />
          VectaPilot
        </a>
        <ul className="flex items-center gap-1 text-sm text-mist-300">
          {LINKS.map((link) => (
            <li key={link.href} className="hidden md:block">
              <a href={link.href} className="rounded-lg px-3 py-2 transition hover:bg-white/5 hover:text-mist-50">
                {link.label}
              </a>
            </li>
          ))}
          <li>
            <a
              href={REPO_URL}
              target="_blank"
              rel="noreferrer"
              className="ml-2 inline-flex items-center gap-2 rounded-full bg-white/5 px-3.5 py-2 text-mist-50 ring-1 ring-white/10 transition hover:bg-white/10"
            >
              <GitHubIcon />
              <span>GitHub</span>
            </a>
          </li>
        </ul>
      </nav>
    </header>
  );
}

function LogoMark() {
  return (
    <svg viewBox="0 0 64 64" className="size-8" aria-hidden>
      <rect width="64" height="64" rx="16" fill="#11143a" />
      <circle cx="32" cy="32" r="21" fill="none" stroke="#818cf8" strokeWidth="2" strokeDasharray="4 5" opacity=".7" />
      <path d="M21 22 L32 44 L43 22" fill="none" stroke="#eef0ff" strokeWidth="5" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="51" cy="20" r="5" fill="#f472b6" />
      <circle cx="13" cy="44" r="4" fill="#22d3ee" />
    </svg>
  );
}

export function GitHubIcon() {
  return (
    <svg viewBox="0 0 16 16" className="size-4" fill="currentColor" aria-hidden>
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}
