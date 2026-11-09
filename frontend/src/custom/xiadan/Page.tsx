import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { PageHeader } from "@/components/PageHeader";
import { priceColorClass } from "@/lib/format";
import { cn } from "@/lib/cn";

/** 持仓价格展示: 保留最多三位小数(ETF 三位)并去掉尾零, 如 4.108 → 4.108、2.90 → 2.9。 */
function fmtHoldingPrice(v?: number | null): string {
  if (v == null || Number.isNaN(v)) return "—";
  return String(Number(v.toFixed(3)));
}

interface XiadanHolding {
  code: string;
  symbol?: string | null;
  name: string;
  qty?: number | null;
  cost_price?: number | null;
  last_price?: number | null;
  numbers: number[];
}

interface XiadanSnapshot {
  window_found: boolean;
  window_title?: string;
  count: number;
  holdings: XiadanHolding[];
  raw_text: string;
  captured_at?: number;
  error?: string;
}

async function fetchHoldings(): Promise<XiadanSnapshot> {
  const res = await fetch("/api/custom/xiadan/holdings");
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body?.detail) message = String(body.detail);
    } catch {
      /* 非 JSON 错误保持默认文案 */
    }
    throw new Error(message);
  }
  return res.json();
}

function CostPnL({
  last,
  cost,
}: {
  last?: number | null;
  cost?: number | null;
}) {
  if (last == null || cost == null || !(cost > 0))
    return <span className="text-muted/60">—</span>;
  const ratio = (last - cost) / cost;
  const pct = (ratio * 100).toFixed(2) + "%";
  return (
    <span className={cn("font-mono", priceColorClass(ratio))}>
      {ratio >= 0 ? "+" : ""}
      {pct}
    </span>
  );
}

export function Xiadan() {
  const [showRaw, setShowRaw] = useState(false);

  const query = useQuery({
    queryKey: ["xiadan-holdings"],
    queryFn: fetchHoldings,
    refetchInterval: 30_000,
  });

  const data = query.data;
  const holdings = data?.holdings ?? [];

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        title="同花顺持仓"
        subtitle="读取同花顺「网上股票交易系统5.0」交易窗口的实时持仓 (截图 + OCR, 只读)"
      />
      <div className="flex-1 min-h-0 px-5 py-4">
        <div className="mx-auto max-w-5xl space-y-4">
          <div className="flex items-center justify-between">
            <div className="text-xs text-secondary">
              {data ? `${data.count} 条持仓` : "—"}
              {data?.captured_at
                ? ` · ${new Date(data.captured_at * 1000).toLocaleTimeString()}`
                : ""}
            </div>
            {data?.window_found && (
              <button
                onClick={() => setShowRaw((v) => !v)}
                className="rounded-btn border border-border px-3 py-1 text-[11px] text-secondary hover:bg-elevated cursor-pointer"
              >
                {showRaw ? "隐藏" : "显示"} OCR 原文
              </button>
            )}
          </div>

          {query.isLoading ? (
            <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center text-xs text-muted">
              读取中…
            </div>
          ) : query.isError ? (
            <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center">
              <div className="text-xs text-danger">
                读取失败: {String((query.error as any)?.message ?? query.error)}
              </div>
              <button
                onClick={() => query.refetch()}
                className="mt-2 inline-flex items-center gap-1 rounded-btn border border-border px-3 py-1 text-[11px] text-secondary hover:bg-elevated cursor-pointer"
              >
                <RefreshCw className="h-3 w-3" />
                重试
              </button>
            </div>
          ) : !data?.window_found ? (
            <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center">
              <div className="text-sm text-muted">
                {data?.error || "未找到同花顺交易窗口"}
              </div>
              <div className="mt-1 text-[11px] text-muted/70">
                请先打开同花顺「网上股票交易系统5.0」并登录到持仓页, 再点重试。
              </div>
              <button
                onClick={() => query.refetch()}
                className="mt-2 inline-flex items-center gap-1 rounded-btn border border-border px-3 py-1 text-[11px] text-secondary hover:bg-elevated cursor-pointer"
              >
                <RefreshCw className="h-3 w-3" />
                重试
              </button>
            </div>
          ) : holdings.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border px-6 py-12 text-center">
              <div className="text-sm text-muted">
                窗口已找到, 但未识别到持仓
              </div>
              <div className="mt-1 text-[11px] text-muted/70">
                可点「显示 OCR 原文」排查识别结果。
              </div>
            </div>
          ) : (
            <div className="overflow-hidden rounded-xl border border-border bg-surface/40 shadow-sm">
              <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                  <thead>
                    <tr className="border-b border-border/60 bg-surface/60 text-[10px] uppercase tracking-wide text-muted">
                      <th className="px-4 py-2 font-medium">名称</th>
                      <th className="px-2 py-2 font-medium text-right">代码</th>
                      <th className="px-2 py-2 font-medium text-right">数量</th>
                      <th className="px-2 py-2 font-medium text-right">
                        成本价
                      </th>
                      <th className="px-2 py-2 font-medium text-right">现价</th>
                      <th className="px-2 py-2 font-medium text-right">
                        盈亏%
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {holdings.map((h, i) => (
                      <tr
                        key={`${h.code}-${i}`}
                        className="border-b border-border/40 last:border-0 hover:bg-elevated/40"
                      >
                        <td className="px-4 py-2.5">
                          <span className="text-foreground">{h.name}</span>
                          {h.symbol && (
                            <span className="ml-1.5 font-mono text-muted">
                              {h.symbol}
                            </span>
                          )}
                        </td>
                        <td className="px-2 py-2.5 text-right font-mono text-secondary">
                          {h.code}
                        </td>
                        <td className="px-2 py-2.5 text-right font-mono text-foreground">
                          {h.qty ?? "—"}
                        </td>
                        <td className="px-2 py-2.5 text-right font-mono text-secondary">
                          {fmtHoldingPrice(h.cost_price)}
                        </td>
                        <td className="px-2 py-2.5 text-right font-mono text-secondary">
                          {fmtHoldingPrice(h.last_price)}
                        </td>
                        <td className="px-2 py-2.5 text-right">
                          <CostPnL last={h.last_price} cost={h.cost_price} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {showRaw && data?.raw_text && (
            <pre className="overflow-x-auto rounded-xl border border-border bg-base px-4 py-3 text-[11px] leading-relaxed text-secondary whitespace-pre-wrap">
              {data.raw_text}
            </pre>
          )}
        </div>
      </div>
    </div>
  );
}
