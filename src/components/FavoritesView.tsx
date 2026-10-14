"use client";

import React, { useEffect, useRef, useState } from "react";
import { Play, Headphones, Pause, Trash2, Tag } from "lucide-react";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import { deletePodcastFavorite, updatePodcastFavoriteTags } from "@/lib/podcast-favorites";
import { getPodcastHistoryItem } from "@/lib/podcast-history";
import type { FavoriteSentence } from "@/types/podcast";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export const FavoritesView = ({ 
  items,
  onNavigateToPodcast,
  onFavoritesChange,
}: { 
  items: FavoriteSentence[];
  onNavigateToPodcast?: (podcastId: string, start: string) => void;
  onFavoritesChange?: () => void;
}) => {
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [selectedPodcastTitle, setSelectedPodcastTitle] = useState<string>("all");
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const podcastOptions = Array.from(new Set(items.map((item) => item.podcastTitle)));

  const filteredItems = items.filter((item) =>
    selectedPodcastTitle === "all" ? true : item.podcastTitle === selectedPodcastTitle,
  );

  const parseTimeToSeconds = (time: string | undefined) => {
    if (!time) {
      return null;
    }
    const parts = time.split(":").map(Number);
    if (parts.some(Number.isNaN)) {
      return null;
    }
    if (parts.length === 2) {
      return parts[0] * 60 + parts[1];
    }
    if (parts.length === 3) {
      return parts[0] * 3600 + parts[1] * 60 + parts[2];
    }
    return null;
  };

  const handleTogglePlay = (item: FavoriteSentence) => {
    if (playingId === item.id) {
      audioRef.current?.pause();
      setPlayingId(null);
      return;
    }

    if (audioRef.current) {
      audioRef.current.pause();
    }

    if (!item.mediaUrl) {
      alert("该收藏项没有可用的音频地址");
      return;
    }

    const audio = new Audio(item.mediaUrl);
    const startSeconds = parseTimeToSeconds(item.start) ?? 0;
    const historyPodcast = getPodcastHistoryItem(item.podcastId);
    const fallbackNextStart = historyPodcast?.segments.find((segment) => segment.start === item.end)?.start;
    const inferredEnd =
      item.end ??
      historyPodcast?.segments[historyPodcast.segments.findIndex((segment) => segment.start === item.start) + 1]?.start;
    const endSeconds =
      parseTimeToSeconds(inferredEnd ?? fallbackNextStart ?? undefined) ??
      Math.min(startSeconds + 6, Number.MAX_SAFE_INTEGER);

    audio.onloadedmetadata = () => {
      audio.currentTime = startSeconds;
      audio.play().catch(err => {
        console.error("Playback error:", err);
        setPlayingId(null);
      });
    };

    audio.ontimeupdate = () => {
      if (endSeconds !== null && audio.currentTime >= endSeconds - 0.05) {
        audio.pause();
        audio.currentTime = startSeconds;
        setPlayingId(null);
      }
    };

    audio.onplay = () => setPlayingId(item.id);
    audio.onpause = () => setPlayingId(null);
    audio.onended = () => setPlayingId(null);

    audioRef.current = audio;
  };

  // 这两个操作现在要打到服务端，所以是异步的。
  // 失败必须让用户看见 —— 静默失败会让「取消收藏」看起来点了没反应，
  // 而实际上只是网络断了，下次刷新那条收藏又回来了。
  const handleDeleteFavorite = async (id: string) => {
    if (playingId === id) {
      audioRef.current?.pause();
      setPlayingId(null);
    }
    try {
      await deletePodcastFavorite(id);
      onFavoritesChange?.();
    } catch (error) {
      alert(error instanceof Error ? error.message : "取消收藏失败，请重试。");
    }
  };

  const handleSaveTags = async (id: string, rawValue: string) => {
    const tags = rawValue
      .split(/[\n,，]+/)
      .map((tag) => tag.trim().replace(/^#/, ""))
      .filter(Boolean);

    try {
      await updatePodcastFavoriteTags(id, tags);
      onFavoritesChange?.();
    } catch (error) {
      alert(error instanceof Error ? error.message : "保存标签失败，请重试。");
    }
  };

  return (
    <div className="flex-1 flex flex-col h-full bg-gray-50 overflow-hidden">
      {/* Header for Favorites */}
      <div className="px-10 py-8 border-b border-gray-100 flex items-center justify-between bg-white shadow-sm">
        <div className="space-y-1">
          <h2 className="text-2xl font-black text-gray-900 tracking-tight">我的收藏</h2>
          <p className="text-xs font-bold text-gray-400 uppercase tracking-widest flex items-center gap-2">
            <span className="w-1.5 h-1.5 bg-blue-500 rounded-full animate-pulse" />
            {filteredItems.length}/{items.length} 条收藏语料
          </p>
        </div>
      </div>

      <div className="border-b border-gray-100 bg-white px-10 py-5">
        <div className="mx-auto flex max-w-7xl items-center gap-3 overflow-x-auto pb-1">
          <button
            onClick={() => setSelectedPodcastTitle("all")}
            className={cn(
              "shrink-0 rounded-full px-4 py-2 text-xs font-bold transition-all",
              selectedPodcastTitle === "all"
                ? "bg-blue-600 text-white shadow-lg shadow-blue-600/20"
                : "bg-gray-100 text-gray-500 hover:bg-gray-200 hover:text-gray-700"
            )}
          >
            全部播客
          </button>
          {podcastOptions.map((podcastTitle) => (
            <button
              key={podcastTitle}
              onClick={() => setSelectedPodcastTitle(podcastTitle)}
              className={cn(
                "shrink-0 rounded-full px-4 py-2 text-xs font-bold transition-all",
                selectedPodcastTitle === podcastTitle
                  ? "bg-blue-600 text-white shadow-lg shadow-blue-600/20"
                  : "bg-gray-100 text-gray-500 hover:bg-gray-200 hover:text-gray-700"
              )}
            >
              {podcastTitle}
            </button>
          ))}
          {selectedPodcastTitle !== "all" && (
            <div className="shrink-0 rounded-full bg-blue-50 px-4 py-2 text-xs font-bold text-blue-600">
              正在查看当前播客
            </div>
          )}
        </div>
      </div>

      {/* Grid of Favorite Cards */}
      <div className="flex-1 overflow-y-auto px-10 py-10 custom-scrollbar">
        <div className="mx-auto grid max-w-7xl grid-cols-1 gap-6 pb-20 md:grid-cols-2 xl:grid-cols-3">
          {filteredItems.map((item) => (
            <FavoriteCard 
              key={item.id} 
              item={item} 
              isPlaying={playingId === item.id}
              onTogglePlay={() => handleTogglePlay(item)}
              onClick={() => onNavigateToPodcast?.(item.podcastId, item.start)}
              onDelete={() => handleDeleteFavorite(item.id)}
              onSaveTags={(value) => handleSaveTags(item.id, value)}
            />
          ))}
          {filteredItems.length === 0 && (
            <div className="col-span-full rounded-[32px] border border-dashed border-gray-200 bg-white px-8 py-16 text-center">
              <p className="text-lg font-black text-gray-700">这个播客下还没有收藏句子</p>
              <p className="mt-3 text-sm font-semibold text-gray-400">切换到其他播客筛选器，或者返回全部播客查看全部收藏。</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};

const FavoriteCard = ({ 
  item, 
  isPlaying,
  onTogglePlay,
  onClick,
  onDelete,
  onSaveTags,
}: { 
  item: FavoriteSentence; 
  isPlaying: boolean;
  onTogglePlay: () => void;
  onClick: () => void;
  onDelete: () => void;
  onSaveTags: (value: string) => void;
}) => {
  const [tagInput, setTagInput] = useState((item.tags ?? []).map((tag) => `#${tag}`).join(" "));

  useEffect(() => {
    setTagInput((item.tags ?? []).map((tag) => `#${tag}`).join(" "));
  }, [item.tags]);

  return (
  <div className="group bg-white border border-gray-100 rounded-[40px] p-10 hover:shadow-2xl hover:shadow-blue-500/10 transition-all duration-500 flex flex-col gap-8 relative overflow-hidden ring-1 ring-gray-100/50">
    {/* Podcast Source Badge */}
    <div className="flex items-start justify-between gap-4">
      <div className="flex items-center gap-2.5 text-[11px] font-black text-blue-500 uppercase tracking-widest min-w-0">
        <Headphones size={14} strokeWidth={2.5} />
        <span className="truncate max-w-[400px] cursor-pointer hover:underline" onClick={onClick}>
          {item.podcastTitle} | {item.start}
        </span>
      </div>

      <button
        onClick={(e) => {
          e.stopPropagation();
          onDelete();
        }}
        className="shrink-0 rounded-2xl border border-gray-100 px-3 py-2 text-gray-400 transition-all hover:border-red-100 hover:bg-red-50 hover:text-red-500 active:scale-95"
        aria-label="删除收藏"
      >
        <Trash2 size={16} />
      </button>
    </div>

    {/* Text Content */}
    <div className="space-y-6 flex-1">
      <p className="text-[22px] font-black text-gray-900 leading-[1.6] tracking-tight group-hover:text-blue-600 transition-colors duration-500">
        {item.original}
      </p>
      <p className="text-[15px] font-bold text-gray-400 leading-relaxed max-w-3xl">
        {item.translation}
      </p>
      <div className="space-y-2">
        <div className="flex items-center gap-2 text-[11px] font-black uppercase tracking-widest text-gray-300">
          <Tag size={13} />
          <span>#标签</span>
        </div>
        <textarea
          value={tagInput}
          onChange={(e) => setTagInput(e.target.value)}
          onBlur={() => onSaveTags(tagInput)}
          placeholder={"#面试\n#英语口语"}
          rows={2}
          className="w-full resize-none rounded-2xl border border-gray-100 bg-gray-50 px-4 py-3 text-sm font-semibold text-gray-600 outline-none transition-all placeholder:text-gray-300 focus:border-blue-200 focus:bg-white focus:text-gray-900"
        />
      </div>
    </div>

    {/* Footer with Play Button */}
    <div className="flex items-center justify-between pt-4 border-t border-gray-50/50">
      <div className="flex items-center gap-4 text-[10px] font-bold text-gray-300 uppercase tracking-[0.2em]">
        <span>Collected on {new Date(item.createdAt).toLocaleDateString()}</span>
      </div>
      <button 
        onClick={(e) => {
          e.stopPropagation();
          onTogglePlay();
        }}
        className={cn(
          "w-16 h-16 rounded-[24px] flex items-center justify-center shadow-2xl transition-all duration-500 active:scale-90 group-hover:translate-y-[-4px]",
          isPlaying 
            ? "bg-gray-900 text-white shadow-gray-900/40" 
            : "bg-blue-600 text-white shadow-blue-600/30 hover:bg-blue-700 hover:shadow-blue-600/40"
        )}
      >
        {isPlaying ? (
          <Pause size={28} className="fill-current" />
        ) : (
          <Play size={28} className="fill-current ml-1" />
        )}
      </button>
    </div>
  </div>
  );
};
