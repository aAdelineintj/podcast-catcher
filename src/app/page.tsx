"use client";

import React, { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { FileSearch, RefreshCw, Upload } from "lucide-react";
import { Sidebar } from "@/components/Sidebar";
import { Header } from "@/components/Header";
import { FavoritesView } from "@/components/FavoritesView";
import {
  cachePodcastItem,
  fetchPodcastHistory,
  loadPodcastHistory,
} from "@/lib/podcast-history";
import { fetchPodcastFavorites, loadPodcastFavorites } from "@/lib/podcast-favorites";
import {
  DEFAULT_SOURCE_LANGUAGE,
  loadSourceLanguage,
  saveSourceLanguage,
} from "@/lib/podcast-settings";
import { apiRequest, uploadFileToSignedUrl } from "@/lib/api";
import type { FavoriteSentence, SourceLanguage, StoredPodcast } from "@/types/podcast";

/** `POST /api/podcasts` 的响应：建好的记录 + 一个可直传的签名地址。 */
type CreatePodcastResponse = {
  podcast: StoredPodcast;
  upload: { path: string; uploadUrl: string; token: string };
};

function HomeContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [view, setView] = useState<"import" | "favorites">("import");
  const [historyItems, setHistoryItems] = useState<StoredPodcast[]>([]);
  const [favoriteItems, setFavoriteItems] = useState<FavoriteSentence[]>([]);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  // 初始值用常量而非直接读 localStorage，避免服务端渲染与客户端不一致（hydration mismatch）
  const [sourceLanguage, setSourceLanguage] = useState<SourceLanguage>(DEFAULT_SOURCE_LANGUAGE);
  const [isProcessing, setIsProcessing] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // 云端同步失败单独记一份：它和「上传失败」的成因完全不同
  // （一个是网络/服务端，一个是文件本身），混在一起会误导排查方向
  const [syncError, setSyncError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const refreshHistory = useCallback(async () => {
    try {
      setHistoryItems(await fetchPodcastHistory());
      setSyncError(null);
    } catch (error) {
      setSyncError(error instanceof Error ? error.message : "读取历史记录失败。");
    }
  }, []);

  const refreshFavorites = useCallback(async () => {
    try {
      setFavoriteItems(await fetchPodcastFavorites());
      setSyncError(null);
    } catch (error) {
      setSyncError(error instanceof Error ? error.message : "读取收藏失败。");
    }
  }, []);

  useEffect(() => {
    // 先用本地缓存渲染，首屏立刻有内容；再拉云端数据覆盖。
    // 顺序不能反 —— 先请求云端的话，那一次往返期间侧栏是空的。
    setHistoryItems(loadPodcastHistory());
    setFavoriteItems(loadPodcastFavorites());
    setSourceLanguage(loadSourceLanguage());
    void refreshHistory();
    void refreshFavorites();
  }, [refreshHistory, refreshFavorites]);

  // 语种选择持久化：按选中语言记住，下次打开沿用（首次进入仍是 DEFAULT_SOURCE_LANGUAGE）
  const handleSourceLanguageChange = (value: SourceLanguage) => {
    setSourceLanguage(value);
    saveSourceLanguage(value);
  };

  useEffect(() => {
    const nextView = searchParams.get("view");
    if (nextView === "favorites") {
      setFavoriteItems(loadPodcastFavorites());
      void refreshFavorites();
      setView("favorites");
      return;
    }

    setView("import");
  }, [searchParams, refreshFavorites]);

  const handleUploadClick = () => {
    fileInputRef.current?.click();
  };

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) {
      setSelectedFile(file);
      setView("import");
      void handleStartProcessing(file);
    }
  };

  /**
   * 上传分三步，且顺序不能换：
   *   ① 建任务 → 服务端插一行 podcasts，并签发上传地址
   *   ② 直传音频 → 浏览器 PUT 到 Storage，字节不经过我们的服务器
   *   ③ 标记完成 → 回填 audio_uploaded_at，此刻 worker 才可能认领
   *
   * 旧版是一句话 `POST /process-podcast` 把文件整个发给 FastAPI。
   * 现在必须拆开：Vercel 的请求体上限只有 4.5MB，几十 MB 的音频
   * 走服务端中转会在**传完之后**才 413 失败 —— 用户白等一场。
   */
  const handleStartProcessing = async (fileToProcess?: File) => {
    const file = fileToProcess || selectedFile;
    if (!file) return;

    setIsProcessing(true);
    setErrorMessage(null);

    try {
      // ① 建任务 + 拿签名上传地址
      const created = await apiRequest<CreatePodcastResponse>("/api/podcasts", {
        method: "POST",
        body: JSON.stringify({ filename: file.name, sourceLanguage }),
      });

      // ② 直传音频到 Storage
      await uploadFileToSignedUrl(created.upload.uploadUrl, file);

      // ③ 标记上传完成
      await apiRequest(`/api/podcasts/${encodeURIComponent(created.podcast.id)}/uploaded`, {
        method: "POST",
        body: JSON.stringify({ bytes: file.size }),
      });

      // 先把服务端返回的记录写进缓存，详情页首屏立刻就有标题
      cachePodcastItem(created.podcast);
      setHistoryItems(loadPodcastHistory());
      setSelectedFile(null);
      router.push(`/podcast/${created.podcast.id}`);
    } catch (error) {
      console.error("上传失败：", error);
      setIsProcessing(false);
      setErrorMessage(error instanceof Error ? error.message : "上传失败，请重试。");
    }
  };

  const handleSelectNowPlaying = () => {
    router.push("/");
    setView("import");
  };

  const handleSelectHistory = (podcast: StoredPodcast) => {
    router.push(`/podcast/${podcast.id}`);
  };

  const handleSelectFavorites = () => {
    setFavoriteItems(loadPodcastFavorites());
    void refreshFavorites();
    router.push("/?view=favorites");
  };

  const handleNavigateToPodcast = (podcastId: string, start: string) => {
    router.push(`/podcast/${podcastId}?t=${start}`);
  };

  return (
    <div className="flex h-screen bg-gray-50 overflow-hidden font-sans">
      <Sidebar
        activeTab={view === "favorites" ? "favorites" : "now-playing"}
        historyItems={historyItems}
        onSelectNowPlaying={handleSelectNowPlaying}
        onSelectHistory={handleSelectHistory}
        onSelectFavorites={handleSelectFavorites}
      />

      <input
        type="file"
        ref={fileInputRef}
        onChange={handleFileChange}
        className="hidden"
        accept="audio/*"
      />

      <main className="flex-1 flex flex-col min-w-0 bg-white relative z-0 shadow-2xl">
        {view !== "favorites" && (
          <Header
            mode="import"
            title=""
            onUploadClick={handleUploadClick}
            isProcessing={isProcessing}
            sourceLanguage={sourceLanguage}
            onSourceLanguageChange={handleSourceLanguageChange}
          />
        )}

        {view === "favorites" ? (
          <FavoritesView 
            items={favoriteItems} 
            onNavigateToPodcast={handleNavigateToPodcast}
            onFavoritesChange={() => void refreshFavorites()}
          />
        ) : (
          <div className="flex-1 flex flex-col items-center justify-center p-8 bg-white overflow-hidden">
            <div className="max-w-md w-full text-center space-y-12 animate-in fade-in duration-700">
              <div className="relative inline-block">
                <div
                  onClick={handleUploadClick}
                  className="w-24 h-24 bg-blue-600 border-2 border-blue-500 rounded-[32px] flex items-center justify-center shadow-2xl shadow-blue-600/20 transition-transform hover:scale-105 duration-300 cursor-pointer"
                >
                  <Upload className="text-white w-10 h-10" strokeWidth={2.5} />
                </div>
              </div>

              <div className="space-y-4">
                <h2 className="text-2xl font-black text-gray-900 tracking-tight">
                  {selectedFile
                    ? `已选择: ${selectedFile.name}`
                    : "Upload a podcast to start"}
                </h2>
                <p className="text-[13px] font-bold text-gray-400 leading-relaxed max-w-sm mx-auto">
                  {selectedFile
                    ? "上传成功后会自动新增一条 History 记录，并跳转到对应播客页面。"
                    : "上传本地 MP3 后，我们会立刻在左侧 History 新增记录，并进入该音频的专属页面。"}
                </p>
                {(errorMessage || syncError) && (
                  <p className="text-sm font-bold text-red-500">{errorMessage || syncError}</p>
                )}
              </div>

              <div className="pt-4 flex flex-col gap-4">
                {selectedFile ? (
                  <button
                    onClick={() => void handleStartProcessing()}
                    disabled={isProcessing}
                    className="w-full px-8 py-4 bg-blue-600 text-white rounded-2xl font-bold text-sm hover:bg-blue-700 transition-all active:scale-[0.98] disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-3"
                  >
                    {isProcessing ? (
                      <>
                        <RefreshCw className="animate-spin" size={18} />
                        创建任务中...
                      </>
                    ) : (
                      "开始处理"
                    )}
                  </button>
                ) : (
                  <button
                    onClick={handleUploadClick}
                    className="w-full px-8 py-4 bg-gray-50 text-gray-500 rounded-2xl font-bold text-sm hover:bg-gray-100 transition-all active:scale-[0.98] flex items-center justify-center gap-2"
                  >
                    <FileSearch size={16} />
                    选择一个音频开始
                  </button>
                )}
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

export default function Home() {
  return (
    <Suspense fallback={null}>
      <HomeContent />
    </Suspense>
  );
}
