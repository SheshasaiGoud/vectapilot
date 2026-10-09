import { REPO_URL } from "@/lib/team";

export function Footer() {
  return (
    <footer className="border-t border-white/5">
      <div className="mx-auto flex max-w-6xl flex-col gap-3 px-4 py-10 text-sm text-mist-400 sm:flex-row sm:items-center sm:justify-between sm:px-6">
        <p>
          Built in public by{" "}
          <a href="https://github.com/SheshasaiGoud" className="text-mist-200 underline-offset-4 hover:underline">
            shesha
          </a>{" "}
          with Claude Code ·{" "}
          <a href={`${REPO_URL}/blob/main/LICENSE`} className="text-mist-200 underline-offset-4 hover:underline">
            MIT licence
          </a>
        </p>
        <p>
          Avatars:{" "}
          <a
            href="https://www.dicebear.com/styles/notionists/"
            className="text-mist-200 underline-offset-4 hover:underline"
          >
            Notionists
          </a>{" "}
          by Zoish (CC0), via DiceBear
        </p>
      </div>
    </footer>
  );
}
