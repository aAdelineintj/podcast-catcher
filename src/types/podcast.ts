"use client";

export type SourceLanguage = "fr" | "en" | "auto";

export type TranscriptSegment = {
  start: string;
  original: string;
  translation: string;
  translation_status?: string;
  detected_lang?: string;
  detection_reason?: string;
};

export type StoredPodcast = {
  id: string;
  title: string;
  date: string;
  duration: string;
  mediaUrl?: string | null;
  status: string;
  stage: string;
  progress: number;
  translatedCount: number;
  totalSegments: number;
  sourceLanguage?: SourceLanguage;
  transcriptionLanguage?: string | null;
  errorMessage?: string | null;
  segments: TranscriptSegment[];
};

export type FavoriteSentence = {
  id: string;
  podcastId: string;
  podcastTitle: string;
  mediaUrl: string | null;
  start: string;
  end?: string;
  original: string;
  translation: string;
  tags?: string[];
  createdAt: string;
};
