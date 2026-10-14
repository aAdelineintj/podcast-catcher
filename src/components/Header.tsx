"use client";

import React, { useEffect, useState } from "react";
import { Upload, Loader2, Pencil, Check, X } from "lucide-react";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import type { SourceLanguage } from "@/types/podcast";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export const Header = ({ 
  mode = "import", 
  title, 
  subtitle,
  onUploadClick,
  isProcessing = false,
  editableTitle = false,
  onSaveTitle,
  sourceLanguage = "auto",
  onSourceLanguageChange,
}: { 
  mode?: "import" | "player",
  title?: string,
  subtitle?: string,
  onUploadClick?: () => void,
  isProcessing?: boolean,
  editableTitle?: boolean,
  onSaveTitle?: (title: string) => void,
  sourceLanguage?: SourceLanguage,
  onSourceLanguageChange?: (value: SourceLanguage) => void,
}) => {
  const hasTitle = title && title.trim() !== "";
  const [isEditingTitle, setIsEditingTitle] = useState(false);
  const [draftTitle, setDraftTitle] = useState(title || "");

  useEffect(() => {
    setDraftTitle(title || "");
  }, [title]);

  const handleSaveTitle = () => {
    const nextTitle = draftTitle.trim();
    if (!nextTitle) {
      setDraftTitle(title || "");
      setIsEditingTitle(false);
      return;
    }
    onSaveTitle?.(nextTitle);
    setIsEditingTitle(false);
  };

  const handleCancelEdit = () => {
    setDraftTitle(title || "");
    setIsEditingTitle(false);
  };

  return (
    <header className="flex flex-col border-b border-gray-100 bg-white shrink-0">
      <div className="flex items-center justify-between px-10 py-5">
        {/* Left Section */}
        <div className="flex flex-col gap-0.5">
          {mode === "import" ? (
            <h1 className="text-xl font-bold text-gray-900 tracking-tight">导入播客</h1>
          ) : (
            hasTitle ? (
              <>
                {isEditingTitle ? (
                  <div className="flex items-center gap-2 max-w-2xl">
                    <input
                      value={draftTitle}
                      onChange={(event) => setDraftTitle(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          handleSaveTitle();
                        }
                        if (event.key === "Escape") {
                          event.preventDefault();
                          handleCancelEdit();
                        }
                      }}
                      autoFocus
                      className="w-full rounded-xl border border-blue-100 bg-blue-50 px-3 py-1.5 text-sm font-bold text-gray-900 outline-none transition-all focus:border-blue-300 focus:bg-white"
                    />
                    <button
                      onClick={handleSaveTitle}
                      className="rounded-full p-2 text-blue-600 hover:bg-blue-50"
                      aria-label="保存播客标题"
                    >
                      <Check size={16} />
                    </button>
                    <button
                      onClick={handleCancelEdit}
                      className="rounded-full p-2 text-gray-400 hover:bg-gray-50 hover:text-gray-600"
                      aria-label="取消修改播客标题"
                    >
                      <X size={16} />
                    </button>
                  </div>
                ) : (
                  <div className="flex items-center gap-2 max-w-2xl">
                    <h1 className="text-sm font-bold text-gray-900 line-clamp-1 max-w-md">
                      {title}
                    </h1>
                    {editableTitle && (
                      <button
                        onClick={() => setIsEditingTitle(true)}
                        className="rounded-full p-1.5 text-gray-300 transition-all hover:bg-gray-50 hover:text-blue-500"
                        aria-label="编辑播客标题"
                      >
                        <Pencil size={14} />
                      </button>
                    )}
                  </div>
                )}
                <p className="text-[10px] font-bold text-gray-400">
                  {subtitle || "Transcription by Whisper (local)"}
                </p>
              </>
            ) : (
              <div className="h-8" /> 
            )
          )}
        </div>

        {/* Right Section */}
        <div className="flex items-center gap-4">
          {(mode === "import" || onSourceLanguageChange) && (
            <div className="flex items-center rounded-full border border-gray-100 bg-gray-50 p-1">
              {[
                { value: "fr", label: "法语" },
                { value: "en", label: "英语" },
                { value: "auto", label: "自动" },
              ].map((option) => {
                const isActive = sourceLanguage === option.value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => onSourceLanguageChange?.(option.value as SourceLanguage)}
                    className={cn(
                      "rounded-full px-3 py-1.5 text-[11px] font-black transition-all",
                      isActive
                        ? "bg-white text-blue-600 shadow-sm"
                        : "text-gray-400 hover:text-gray-700"
                    )}
                  >
                    {option.label}
                  </button>
                );
              })}
            </div>
          )}

          <button 
            onClick={onUploadClick}
            disabled={isProcessing}
            className={cn(
              "flex items-center gap-2 px-6 py-2.5 bg-blue-600 text-white rounded-full font-bold text-xs transition-all shadow-lg shadow-blue-600/20 active:scale-[0.98]",
              isProcessing ? "cursor-not-allowed opacity-60" : "hover:bg-blue-700"
            )}
          >
            {isProcessing ? (
              <Loader2 size={16} strokeWidth={3} className="animate-spin" />
            ) : (
              <Upload size={16} strokeWidth={3} />
            )}
            <span>{isProcessing ? "Processing..." : "Local MP3"}</span>
          </button>
        </div>
      </div>
    </header>
  );
};
