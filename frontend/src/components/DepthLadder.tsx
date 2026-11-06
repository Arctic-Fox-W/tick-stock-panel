/**
 * 五档盘口 (卖五~卖一 / 买一~买五)。
 *
 * 详情弹窗按需查询, 支持任意标的 (含 ETF); 交易时段按时长定时刷新, 非交易时段仅手动刷新。
 * 已购能力门禁由调用方判定, 本组件不再重复判断。
 */
import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtVolume } from '@/lib/format'

const LEVELS = 5

interface Props {
  symbol: string
  /** 交易时段内的刷新间隔(ms); 非交易时段不自动刷新 */
  refetchIntervalMs?: number
  /** 是否处于交易时段 — 决定是否定时刷新 */
  trading?: boolean
  className?: string
}

/** 价格小数位: 整分显示 2 位, 含三厘(如 ETF 3.512) 显示 3 位。 */
function priceText(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return Number.isInteger(v * 100) ? v.toFixed(2) : v.toFixed(3)
}

/** 取数时间: epoch 秒 → HH:MM:SS (本地时区)。 */
function fmtClock(asofSec: number): string {
  const d = new Date(asofSec * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

function LevelRow({ label, price, vol, side, maxVol }: {
  label: string
  price: number
  vol: number
  side: 'ask' | 'bid'
  maxVol: number
}) {
  const pct = Math.max(2, Math.round((vol / maxVol) * 100))
  return (
    <div className="relative grid grid-cols-[22px_1fr_auto] items-center gap-1.5 rounded-sm px-1.5 py-[3px] font-mono text-[11px] tabular-nums">
      <span
        aria-hidden
        className={cn('absolute inset-y-0 right-0 rounded-sm', side === 'ask' ? 'bg-bear/12' : 'bg-bull/12')}
        style={{ width: `${pct}%` }}
      />
      <span className="relative text-muted">{label}</span>
      <span className={cn('relative text-right', side === 'ask' ? 'text-bear' : 'text-bull')}>
        {priceText(price)}
      </span>
      <span className="relative min-w-[52px] text-right text-secondary">{fmtVolume(vol)}</span>
    </div>
  )
}

export function DepthLadder({ symbol, refetchIntervalMs = 6000, trading = false, className }: Props) {
  const q = useQuery({
    queryKey: QK.depthBook(symbol),
    queryFn: () => api.depth(symbol),
    refetchInterval: trading ? refetchIntervalMs : false,
    staleTime: 2_000,
  })

  const data = q.data
  const asks = (data?.asks ?? []).slice(0, LEVELS)
  const bids = (data?.bids ?? []).slice(0, LEVELS)
  const askVolumes = data?.ask_volumes ?? asks.map(l => l[1])
  const bidVolumes = data?.bid_volumes ?? bids.map(l => l[1])
  const maxVol = Math.max(1, ...askVolumes, ...bidVolumes)
  // 卖盘倒序: 卖五 在最上、卖一 紧邻中间价。
  const askOrder = asks.map((_, i) => i).reverse()

  return (
    <div className={cn('rounded border border-border/60 bg-base/30', className)}>
      <div className="flex items-center justify-between border-b border-border/60 px-2.5 py-1.5">
        <span className="text-[11px] font-medium text-secondary">五档盘口</span>
        <button
          type="button"
          onClick={() => q.refetch()}
          disabled={q.isFetching}
          className="rounded p-0.5 text-muted transition-colors hover:bg-elevated hover:text-foreground disabled:opacity-50"
          title="刷新盘口"
          aria-label="刷新盘口"
        >
          <RefreshCw className={cn('h-3 w-3', q.isFetching && 'animate-spin')} />
        </button>
      </div>

      {!data ? (
        <div className="px-2.5 py-3 text-center text-[11px] text-muted">加载中…</div>
      ) : data.available === false ? (
        <div className="px-2.5 py-3 text-center text-[11px] text-muted">{data.reason ?? '暂无盘口数据'}</div>
      ) : asks.length === 0 && bids.length === 0 ? (
        <div className="px-2.5 py-3 text-center text-[11px] text-muted">暂无盘口数据</div>
      ) : (
        <>
          <div className="flex flex-col py-1">
            {askOrder.map(i => (
              <LevelRow
                key={`ask-${i}`}
                label={`卖${i + 1}`}
                price={asks[i][0]}
                vol={askVolumes[i] ?? asks[i][1]}
                side="ask"
                maxVol={maxVol}
              />
            ))}
            <div className="my-0.5 border-t border-border/50" />
            {bids.map((level, i) => (
              <LevelRow
                key={`bid-${i}`}
                label={`买${i + 1}`}
                price={level[0]}
                vol={bidVolumes[i] ?? level[1]}
                side="bid"
                maxVol={maxVol}
              />
            ))}
          </div>
          {data.asof != null && (
            <div className="border-t border-border/60 px-2.5 py-1 text-center text-[10px] text-muted">
              获取时间 {fmtClock(data.asof)}
            </div>
          )}
        </>
      )}
    </div>
  )
}
