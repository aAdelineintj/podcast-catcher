"use client";

import React, { useEffect, useRef } from "react";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

interface TranscriptLine {
  start: string;
  original: string;
  translation: string;
  translation_status?: string;
  detected_lang?: string;
  active?: boolean;
}

export const Transcription = ({ 
  lines,
  onSeek
}: { 
  lines: TranscriptLine[];
  onSeek?: (time: string) => void;
}) => {
  return (
    <div className="flex-1 overflow-y-auto px-10 py-10 space-y-12 scroll-smooth custom-scrollbar bg-white">
      {lines.map((line, index) => (
        <TranscriptItem 
          key={index} 
          {...line} 
          onClick={() => onSeek?.(line.start)}
        />
      ))}
      <div className="h-40" /> {/* Bottom spacing for better scrolling */}
    </div>
  );
};

const TranscriptItem = ({
  start,
  original,
  translation,
  translation_status,
  detected_lang,
  active = false,
  onClick
}: TranscriptLine & { onClick?: () => void }) => {
  const itemRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (active && itemRef.current) {
      itemRef.current.scrollIntoView({
        behavior: "smooth",
        block: "center",
      });
    }
  }, [active]);

  return (
    <div 
      ref={itemRef}
      onClick={onClick}
      className={cn(
        "relative flex items-start gap-12 group transition-all duration-500 cursor-pointer",
        active ? "opacity-100 translate-x-2" : "opacity-40 hover:opacity-70"
      )}
    >
      {/* Timestamp */}
      <div className={cn(
        "w-16 pt-1 text-[11px] font-bold font-mono shrink-0 transition-colors tracking-widest",
        active ? "text-blue-600" : "text-gray-300"
      )}>
        {start}
      </div>

      {/* Text Content */}
      <div className="flex-1 grid grid-cols-2 gap-16">
        <div className={cn(
          "text-[15px] font-bold leading-[1.8] transition-all",
          active ? "text-gray-900 scale-[1.02] origin-left" : "text-gray-500"
        )}>
          {original}
          {detected_lang && (
            <span className="ml-3 inline-flex rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-black uppercase tracking-wider text-blue-600">
              {detected_lang}
            </span>
          )}
        </div>
        <div className={cn(
          "text-[15px] font-bold leading-[1.8] transition-all",
          active ? "text-gray-900 scale-[1.02] origin-left" : "text-gray-500"
        )}>
          {translation ? (
            translation
          ) : (
            <span className="inline-flex items-center gap-2 text-gray-300">
              <span className="h-2 w-2 rounded-full bg-blue-300 animate-pulse" />
              {translation_status === "pending" ? "翻译中..." : "等待翻译..."}
            </span>
          )}
        </div>
      </div>
    </div>
  );
};
