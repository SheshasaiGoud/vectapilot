"use client";

import { Avatar } from "@/components/team/Avatar";
import { useTeamDialog } from "@/components/team/TeamDialogProvider";
import type { MilestoneOwner } from "@/lib/types";

/** "Maya" · "Ananya & Noah" · "Maya, Diego & Sofia" */
function joinNames(names: string[]) {
  return names.length <= 1 ? names.join("") : `${names.slice(0, -1).join(", ")} & ${names.at(-1)}`;
}

/** Overlapping owner avatars on a roadmap card; each opens that teammate's brief. */
export function OwnerAvatars({ owners }: { owners: MilestoneOwner[] }) {
  const { open } = useTeamDialog();
  return (
    <div className="flex items-center gap-2">
      <ul className="flex -space-x-2">
        {owners.map((owner) => (
          <li key={owner.id}>
            <button
              type="button"
              onClick={() => open(owner.id)}
              aria-haspopup="dialog"
              aria-label={`Open ${owner.firstName}'s brief`}
              className="block w-8 overflow-hidden rounded-full ring-2 ring-ink-900 transition-transform hover:z-10 hover:-translate-y-1 hover:scale-110"
              style={{ boxShadow: `0 0 0 3px ${owner.accent}55` }}
            >
              <Avatar src={owner.avatarSrc} size={32} />
            </button>
          </li>
        ))}
      </ul>
      <span className="text-xs text-mist-400">{joinNames(owners.map((o) => o.firstName))}</span>
    </div>
  );
}
