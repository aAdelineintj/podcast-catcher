"use client";

import React from "react";
import { Headphones, Heart, MessageSquare } from "lucide-react";
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";
import type { StoredPodcast } from "@/types/podcast";

function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export interface PodcastItem {
  id: string;
  title: string;
  date: string;
  duration: string;
}

export const Sidebar = ({ 
  activeTab = "now-playing",
  selectedPodcastId,
  historyItems = [],
  onSelectNowPlaying,
  onSelectHistory,
  onSelectFavorites
}: { 
  activeTab?: string;
  selectedPodcastId?: string | null;
  historyItems?: StoredPodcast[];
  onSelectNowPlaying?: () => void;
  onSelectHistory?: (podcast: StoredPodcast) => void;
  onSelectFavorites?: () => void;
}) => {
  return (
    <aside className="w-64 border-r border-gray-100 flex flex-col h-screen bg-white">
      {/* Logo Section */}
      <div className="p-6 flex items-center gap-3">
        <div className="w-10 h-10 bg-blue-600 rounded-xl flex items-center justify-center shadow-lg shadow-blue-600/20">
          <Headphones className="text-white w-6 h-6" />
        </div>
        <span className="font-bold text-xl text-gray-900 tracking-tight">PodcastCatcher</span>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-4 overflow-y-auto pt-2">
        <div className="mb-8">
          <div 
            onClick={onSelectNowPlaying}
            className={cn(
              "px-4 py-3 rounded-xl flex items-center gap-3 cursor-pointer transition-all",
              activeTab === "now-playing" ? "bg-blue-50 text-blue-600 shadow-sm" : "text-gray-500 hover:bg-gray-50"
            )}
          >
            <Headphones size={20} strokeWidth={activeTab === "now-playing" ? 2.5 : 2} />
            <span className="font-bold">Now Playing</span>
          </div>
        </div>

        <div className="mb-8">
          <h3 className="px-4 text-[10px] font-bold text-gray-400 uppercase tracking-[0.2em] mb-4">
            HISTORY
          </h3>
          <div className="space-y-1">
            {historyItems.map((item) => (
              <HistoryCard
                key={item.id}
                item={item}
                active={selectedPodcastId === item.id && activeTab === "now-playing"}
                onClick={() => onSelectHistory?.(item)}
              />
            ))}
            {historyItems.length === 0 && (
              <div className="px-4 py-3 text-xs font-bold text-gray-300">
                还没有上传记录
              </div>
            )}
          </div>
        </div>
      </nav>

      {/* Footer / Favorites */}
      <div className="p-4 border-t border-gray-100 bg-white">
        <button 
          onClick={onSelectFavorites}
          className={cn(
            "w-full flex items-center gap-3 px-5 py-3.5 rounded-2xl transition-all active:scale-[0.98] mb-3",
            activeTab === "favorites" ? "bg-blue-50 text-blue-600 shadow-sm" : "text-gray-600 hover:bg-gray-50"
          )}
        >
          <Heart size={20} strokeWidth={activeTab === "favorites" ? 2.5 : 2} />
          <span className="font-bold text-sm">我的收藏</span>
        </button>

        <div className="text-center space-y-1 pb-2">
          <p className="text-[10px] font-medium text-gray-400">开发者日常在线反馈Bug</p>
          <a href="#" className="text-[10px] font-bold text-gray-500 hover:text-blue-500 flex items-center justify-center gap-1.5 transition-colors">
             <MessageSquare size={10} />
             MaydayAzure
          </a>
        </div>
      </div>
    </aside>
  );
};

const HistoryCard = ({ item, active = false, onClick }: { 
  item: any; 
  active?: boolean;
  onClick: () => void;
}) => {
  // 格式化日期逻辑
  const formattedDate = React.useMemo(() => {
    if (!item.date) return "";
    try {
      const date = new Date(item.date);
      if (isNaN(date.getTime())) return item.date;
      return date.toLocaleDateString("zh-CN").replace(/\//g, "-");
    } catch {
      return item.date;
    }
  }, [item.date]);

  return (
    <div 
      onClick={onClick}
      className={cn(
        "p-4 rounded-2xl cursor-pointer transition-all border-2",
        active ? "border-blue-500 bg-white shadow-md shadow-blue-500/5" : "border-transparent hover:bg-gray-50"
      )}
    >
      <h4 className={cn(
        "text-sm font-bold mb-2 leading-tight line-clamp-2",
        active ? "text-gray-900" : "text-gray-500"
      )}>
        {item.title}
      </h4>
      <div className="flex items-center gap-3 text-[10px] font-medium text-gray-400">
        <span>{formattedDate}</span>
        <span>•</span>
        <span>{item.duration}</span>
        {item.status !== "success" && (
          <>
            <span>•</span>
            <span>{item.stage}</span>
          </>
        )}
      </div>
    </div>
  );
};
