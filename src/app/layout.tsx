import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "PodcastCatcher - Bilingual Podcast Player",
  description: "Learn languages by listening to podcasts with bilingual transcripts.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
