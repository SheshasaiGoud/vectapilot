import { Footer } from "@/components/sections/Footer";
import { Hero } from "@/components/sections/Hero";
import { HowWeWork } from "@/components/sections/HowWeWork";
import { Nav } from "@/components/sections/Nav";
import { Roadmap } from "@/components/sections/Roadmap";
import { TeamSection } from "@/components/sections/TeamSection";
import { TeamDialogProvider } from "@/components/team/TeamDialogProvider";
import { getTeamData } from "@/lib/team";

export default function Home() {
  // Runs once at build time: reads .claude/agents/, validates, and bakes the result into HTML.
  const { team, human, roadmap } = getTeamData();

  return (
    <TeamDialogProvider team={team}>
      <Nav />
      <main id="top">
        <Hero team={team} human={human} />
        <HowWeWork />
        <TeamSection team={team} />
        <Roadmap roadmap={roadmap} />
      </main>
      <Footer />
    </TeamDialogProvider>
  );
}
