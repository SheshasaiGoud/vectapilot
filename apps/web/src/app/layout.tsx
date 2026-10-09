import type { Metadata } from "next";
import { Inter, Space_Grotesk } from "next/font/google";

import { MotionProvider } from "@/components/motion/MotionProvider";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });
const spaceGrotesk = Space_Grotesk({ subsets: ["latin"], variable: "--font-space-grotesk" });

export const metadata: Metadata = {
  title: "VectaPilot — Meet the crew",
  description:
    "Eight AI teammates and one human lead building VectaPilot, a supervised AI support copilot with grounded answers, safe tools and evals in CI.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${inter.variable} ${spaceGrotesk.variable}`}>
      <body className="font-sans">
        <MotionProvider>{children}</MotionProvider>
      </body>
    </html>
  );
}
