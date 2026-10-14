"use client";

import React, { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { FileSearch, RefreshCw } from "lucide-react";
import { Sidebar } from "@/components/Sidebar";
import { Header } from "@/components/Header";
import { Transcription } from "@/components/Transcription";
import { BottomPlayer, type BottomPlayerRef } from "@/components/BottomPlayer";
import {
  fetchPodcastDetail,
  loadPodcastHistory,
  renamePodcast,
} from "@/lib/podcast-history";
import {
  buildFavoriteId,
  createPodcastFavorite,
  hasPodcastFavorite,
} from "@/lib/podcast-favorites";
import { ApiError } from "@/lib/api";
import type { FavoriteSentence, StoredPodcast } from "@/types/podcast";

const stageTextMap: Record<string, string> = {
  idle: "等待上传",
  uploading: "上传中",
  uploaded: "上传完成",
  queued: "任务排队中",
  transcribing: "正在转写原文",
  transcribed: "转写完成，准备翻译",
  translating: "正在逐句翻译",
  completed: "处理完成",
  failed: "处理失败",
};

function PodcastDetailContent() {
  const params = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const router = useRouter();
  const podcastId = params.id;
  const startTimeFromUrl = searchParams.get("t");
  const [historyItems, setHistoryItems] = useState<StoredPodcast[]>([]);
  const [podcast, setPodcast] = useState<StoredPodcast | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // 任务状态相关的错误单独存一份：它和播放器报错的生命周期不同，
  // 混用一个 state 会让轮询成功时把播放器报错一起清掉。
  const [statusError, setStatusError] = useState<string | null>(null);
  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollFailureRef = useRef(0);
  const podcastRef = useRef<StoredPodcast | null>(null);
  const playerRef = useRef<BottomPlayerRef>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [favoriteRefreshKey, setFavoriteRefreshKey] = useState(0);

  const parseTimeToSeconds = (timeStr: string): number => {
    const parts = timeStr.split(":").map(Number);
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    return 0;
  };

  const activeIndex = useMemo(() => {
    if (!podcast || podcast.segments.length === 0) return -1;
    let index = -1;
    for (let i = 0; i < podcast.segments.length; i++) {
      const segmentStart = parseTimeToSeconds(podcast.segments[i].start);
      if (currentTime >= segmentStart) {
        index = i;
      } else {
        break;
      }
    }
    return index;
  }, [podcast, currentTime]);

  const linesWithActive = useMemo(() => {
    return (podcast?.segments ?? []).map((line, index) => ({
      ...line,
      active: index === activeIndex,
    }));
  }, [podcast, activeIndex]);

  const currentOriginalText = useMemo(() => {
    if (!podcast || podcast.segments.length === 0) {
      return "";
    }
    if (activeIndex >= 0 && podcast.segments[activeIndex]) {
      return podcast.segments[activeIndex].original;
    }
    return podcast.segments[0]?.original || "";
  }, [podcast, activeIndex]);

  // 音频地址由服务端给出（`/api/podcasts/<id>/media`），它每次现签再 302 过去。
  // 旧版这里有个 `|| apiUrl('/jobs/<id>/media')` 的兜底是给 FastAPI 用的；
  // 迁移后那个路径不存在了，留着兜底只会掩盖「mediaUrl 为 null」这个真实状态 ——
  // 而 null 的含义很明确：音频还没传完，现在不该播。
  const resolvedAudioSrc = podcast?.mediaUrl ?? null;

  const currentFavoritePayload = useMemo<FavoriteSentence | null>(() => {
    if (!podcast || podcast.segments.length === 0) {
      return null;
    }

    const targetSegment =
      activeIndex >= 0 && podcast.segments[activeIndex]
        ? podcast.segments[activeIndex]
        : podcast.segments[0];
    const targetIndex =
      activeIndex >= 0 && podcast.segments[activeIndex]
        ? activeIndex
        : 0;

    if (!targetSegment) {
      return null;
    }

    const nextSegment = podcast.segments[targetIndex + 1];

    return {
      id: buildFavoriteId(podcast.id, targetSegment.start),
      podcastId: podcast.id,
      podcastTitle: podcast.title,
      mediaUrl: resolvedAudioSrc,
      start: targetSegment.start,
      end: nextSegment?.start ?? podcast.duration,
      original: targetSegment.original,
      translation: targetSegment.translation,
      createdAt: new Date().toISOString(),
    };
  }, [activeIndex, podcast, resolvedAudioSrc]);

  const isCurrentFavorited = useMemo(() => {
    if (!currentFavoritePayload) {
      return false;
    }
    return hasPodcastFavorite(currentFavoritePayload.id);
  }, [currentFavoritePayload, favoriteRefreshKey]);

  // Handle initial seek from URL
  useEffect(() => {
    if (podcast && startTimeFromUrl && playerRef.current) {
      // Small delay to ensure player is ready
      const timer = setTimeout(() => {
        playerRef.current?.seek(startTimeFromUrl);
      }, 500);
      return () => clearTimeout(timer);
    }
  }, [podcast, startTimeFromUrl]);

  useEffect(() => {
    if (!toastMessage) {
      return;
    }
    const timer = window.setTimeout(() => {
      setToastMessage(null);
    }, 1800);
    return () => window.clearTimeout(timer);
  }, [toastMessage]);

  useEffect(() => {
    podcastRef.current = podcast;
  }, [podcast]);

  useEffect(() => {
    if (!podcastId) {
      return;
    }

    // 先用缓存渲染 —— 从首页点进来时缓存里已经有这条记录了，
    // 直接显示比「先转圈再出内容」体验好得多
    const history = loadPodcastHistory();
    setHistoryItems(history);
    const cached = history.find((item) => item.id === podcastId) ?? null;
    setPodcast(cached);
    podcastRef.current = cached;

    // 再拉云端权威数据。这一步不能省：缓存可能是几分钟前的，
    // 而 worker 在这期间可能已经把任务跑完了。
    let cancelled = false;
    void (async () => {
      try {
        const detail = await fetchPodcastDetail(podcastId);
        if (cancelled) {
          return;
        }
        podcastRef.current = detail;
        setPodcast(detail);
        setHistoryItems(loadPodcastHistory());
        setStatusError(null);
      } catch (error) {
        if (cancelled) {
          return;
        }
        setStatusError(
          error instanceof ApiError && error.status === 404
            ? "这条播客记录不存在或已被删除。"
            : error instanceof Error
              ? error.message
              : "读取播客详情失败。",
        );
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [podcastId]);

  useEffect(() => {
    if (!podcastId) {
      return;
    }

    pollFailureRef.current = 0;

    const stopPolling = () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };

    const isSettled = () => {
      const current = podcastRef.current;
      return !!current && (current.status === "success" || current.status === "failed");
    };

    const applyPodcast = (next: StoredPodcast) => {
      // 标题不再需要「本地优先」的保护：改标题现在直接 PATCH 到服务端，
      // 所以服务端返回的就是最新值。旧版之所以要护着，是因为标题只存在
      // localStorage 里，轮询会用旧的 filename 把它冲掉。
      podcastRef.current = next;
      setPodcast(next);
      setHistoryItems(loadPodcastHistory());

      if (next.status === "success" || next.status === "failed") {
        stopPolling();
      }
    };

    const fetchStatus = async () => {
      applyPodcast(await fetchPodcastDetail(podcastId));
    };

    const handlePollFailure = (error: unknown) => {
      const isNotFound = error instanceof ApiError && error.status === 404;
      const message = isNotFound
        ? "这条播客记录已被删除。"
        : error instanceof Error
          ? error.message
          : "读取任务状态失败。";

      pollFailureRef.current += 1;
      // 关键：必须把失败变成用户可见的状态，否则页面会静默停在「处理中」
      setStatusError(
        pollFailureRef.current >= 3
          ? `${message} 已停止自动刷新。`
          : `${message}（第 ${pollFailureRef.current} 次重试…）`,
      );

      // 404 说明记录真的没了，重试多少次都是 404，立刻停
      if (isNotFound || pollFailureRef.current >= 3) {
        stopPolling();
      }
    };

    const pollOnce = () =>
      fetchStatus()
        .then(() => {
          pollFailureRef.current = 0;
          setStatusError(null);
        })
        .catch(handlePollFailure);

    if (!isSettled()) {
      void pollOnce();
      // 2 秒一次。旧版是 1.5 秒 —— 那时轮询打的是本机 FastAPI，几乎零成本；
      // 现在每次都要经过公网到 Supabase，而免费版有请求配额。
      // 转写动辄几分钟，多等 0.5 秒无感，省下的请求量却很可观。
      pollTimerRef.current = setInterval(() => {
        if (isSettled()) {
          stopPolling();
          return;
        }
        void pollOnce();
      }, 2000);
    }

    return () => {
      stopPolling();
    };
  }, [podcastId]);

  const currentStageText = useMemo(() => {
    if (!podcast) {
      return "载入中";
    }
    return stageTextMap[podcast.stage] || podcast.stage;
  }, [podcast]);

  const lines = podcast?.segments ?? [];

  const handleFavoriteCurrent = async () => {
    if (!currentFavoritePayload) {
      setToastMessage("当前没有可收藏的句子。");
      return;
    }

    try {
      await createPodcastFavorite(currentFavoritePayload);
      setFavoriteRefreshKey((value) => value + 1);
      setToastMessage("已收藏当前句子。");
    } catch (error) {
      // 收藏失败必须说清原因。静默失败时用户会以为收藏成功了，
      // 下次打开收藏夹发现没有，却不知道是哪一步出了问题。
      setToastMessage(error instanceof Error ? error.message : "收藏失败，请重试。");
    }
  };

  const handlePlayerTimeUpdate = (time: number) => {
    setCurrentTime(time);
  };

  useEffect(() => {
    const isEditableElement = (target: EventTarget | null) => {
      const element = target as HTMLElement | null;
      if (!element) {
        return false;
      }

      const tagName = element.tagName;
      return (
        element.isContentEditable ||
        tagName === "INPUT" ||
        tagName === "TEXTAREA" ||
        tagName === "SELECT"
      );
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (isEditableElement(event.target)) {
        return;
      }

      if (event.code === "Space") {
        event.preventDefault();
        playerRef.current?.togglePlayPause();
        return;
      }

      if (event.key === "Enter") {
        event.preventDefault();
        handleFavoriteCurrent();
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [currentFavoritePayload, favoriteRefreshKey]);

  const handleSelectNowPlaying = () => {
    router.push("/");
  };

  const handleSelectHistory = (item: StoredPodcast) => {
    router.push(`/podcast/${item.id}`);
  };

  const handleSelectFavorites = () => {
    router.push("/?view=favorites");
  };

  const handleSavePodcastTitle = async (title: string) => {
    if (!podcast) {
      return;
    }

    try {
      // 以服务端返回的记录为准，而不是本地拼一个 {...podcast, title}。
      // 因为收藏列表里的播客名是从 podcasts 表实时带出来的，
      // 本地拼的版本会和服务端产生不一致。
      const updated = await renamePodcast(podcast.id, title);
      podcastRef.current = updated;
      setPodcast(updated);
      setHistoryItems(loadPodcastHistory());
      setFavoriteRefreshKey((value) => value + 1);
      setToastMessage("已更新当前播客标题。");
    } catch (error) {
      setToastMessage(error instanceof Error ? error.message : "更新标题失败，请重试。");
    }
  };

  const handleSeekFromTranscript = (time: string) => {
    playerRef.current?.seek(time);
  };

  return (
    <div className="flex h-screen bg-gray-50 overflow-hidden font-sans">
      <Sidebar
        activeTab="now-playing"
        selectedPodcastId={podcastId}
        historyItems={historyItems}
        onSelectNowPlaying={handleSelectNowPlaying}
        onSelectHistory={handleSelectHistory}
        onSelectFavorites={handleSelectFavorites}
      />

      <main className="flex-1 flex flex-col min-w-0 bg-white relative z-0 shadow-2xl">
        {toastMessage && (
          <div className="fixed bottom-30 left-[55%] z-[120] -translate-x-1/2 rounded-full border border-white/20 bg-gray-900/30 px-4 py-1.5 text-[13px] font-semibold tracking-[0.01em] text-white shadow-lg shadow-black/10 backdrop-blur-lg">
            {toastMessage}
          </div>
        )}

        <Header
          mode="player"
          title={podcast?.title || "Podcast"}
          subtitle={`${currentStageText}${podcast && podcast.totalSegments > 0 ? ` · ${podcast.translatedCount}/${podcast.totalSegments}` : ""}`}
          editableTitle
          onSaveTitle={handleSavePodcastTitle}
        />

        {!podcast ? (
          <div className="flex-1 flex flex-col items-center justify-center p-8 bg-white text-center">
            <div className="space-y-4">
              <div className="w-20 h-20 bg-gray-50 rounded-[24px] flex items-center justify-center mx-auto border border-gray-100 shadow-inner">
                <FileSearch className="text-gray-300 w-10 h-10" />
              </div>
              <h3 className="text-xl font-black text-gray-900">没有找到这条播客记录</h3>
              <p className="text-sm font-bold text-gray-400">可以返回首页重新上传一个音频。</p>
            </div>
          </div>
        ) : (
          <>
            {(podcast.errorMessage || statusError) && (
              <div className="mx-10 mt-4 rounded-2xl border border-red-100 bg-red-50 px-5 py-4">
                <p className="text-sm font-bold text-red-600">
                  {podcast.errorMessage || statusError}
                </p>
                <p className="mt-1 text-xs font-bold text-red-400">
                  可返回首页重新上传该音频。
                </p>
              </div>
            )}
            {errorMessage && (
              <div className="px-10 pt-4 text-sm font-bold text-red-500">{errorMessage}</div>
            )}
            {lines.length === 0 && !podcast.errorMessage && !statusError ? (
              <div className="flex-1 flex flex-col items-center justify-center p-8 bg-white text-center">
                <div className="w-16 h-16 rounded-[20px] bg-blue-50 border border-blue-100 flex items-center justify-center">
                  <RefreshCw className="text-blue-400 w-7 h-7 animate-spin" />
                </div>
                <h3 className="mt-5 text-lg font-black text-gray-900">{currentStageText}</h3>
                <p className="mt-2 text-[13px] font-bold text-gray-400 max-w-sm">
                  {podcast.totalSegments > 0
                    ? `已处理 ${podcast.translatedCount}/${podcast.totalSegments} 句，字幕会逐步出现。`
                    : "正在等待后端返回第一批字幕，请保持本页打开。"}
                </p>
              </div>
            ) : (
              <Transcription
                lines={linesWithActive}
                onSeek={handleSeekFromTranscript}
              />
            )}
            <BottomPlayer
              ref={playerRef}
              audioSrc={resolvedAudioSrc}
              title={podcast.title}
              onTimeUpdate={handlePlayerTimeUpdate}
              onPlaybackError={setErrorMessage}
              copySourceText={currentOriginalText}
              onCopyResult={setToastMessage}
              onFavoriteCurrent={handleFavoriteCurrent}
              isCurrentFavorited={isCurrentFavorited}
            />
          </>
        )}
      </main>
    </div>
  );
}

export default function PodcastDetailPage() {
  return (
    <Suspense fallback={null}>
      <PodcastDetailContent />
    </Suspense>
  );
}
