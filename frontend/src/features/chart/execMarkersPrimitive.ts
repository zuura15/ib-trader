/**
 * Execution-fill dots drawn as a lightweight-charts series primitive.
 *
 * The native series-markers plugin clamps circles to a ~8px minimum
 * diameter (it scales with bar spacing, floor 12px × 0.8), which is too
 * large next to a 2px price line. This primitive sizes each dot as a
 * multiple of the series line width (operator setting, 1–10×) and
 * reports hits by id so the crosshair's ``hoveredInfo.objectId`` drives
 * the hover readout exactly as the native markers did.
 */
import type {
  IChartApi, IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi,
  ISeriesPrimitive, PrimitiveHoveredItem, SeriesAttachedParameter,
  SeriesType, Time,
} from 'lightweight-charts';

export interface ExecDot {
  id: string;
  time: Time;
  price: number;
  color: string;
}

type DrawTarget = Parameters<IPrimitivePaneRenderer['draw']>[0];

/** Hover target radius floor (CSS px) — a 2–4px dot is otherwise
 *  nearly impossible to land the pointer on. */
const MIN_HIT_RADIUS = 5;

export class ExecDotsPrimitive implements ISeriesPrimitive<Time> {
  private chart: IChartApi | null = null;
  private series: ISeriesApi<SeriesType> | null = null;
  private requestUpdate: (() => void) | null = null;
  private dots: ExecDot[] = [];
  private scale: number;
  /** Screen positions from the last paint, reused for hit-testing. */
  private placed: Array<{ id: string; x: number; y: number; color: string }> = [];

  constructor(scale: number) {
    this.scale = scale;
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.chart = param.chart as IChartApi;
    this.series = param.series;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.series = null;
    this.requestUpdate = null;
  }

  setDots(dots: ExecDot[]): void {
    this.dots = dots;
    this.requestUpdate?.();
  }

  setScale(scale: number): void {
    this.scale = scale;
    this.requestUpdate?.();
  }

  /** Dot diameter in CSS px: ``scale`` × the series line width. */
  private diameter(): number {
    const lw = Number(
      (this.series?.options() as { lineWidth?: number } | undefined)?.lineWidth,
    );
    return this.scale * (Number.isFinite(lw) && lw > 0 ? lw : 2);
  }

  updateAllViews(): void {
    const chart = this.chart;
    const series = this.series;
    this.placed = [];
    if (!chart || !series) return;
    const ts = chart.timeScale();
    for (const d of this.dots) {
      const x = ts.timeToCoordinate(d.time);
      const y = series.priceToCoordinate(d.price);
      if (x == null || y == null) continue;
      this.placed.push({ id: d.id, x, y, color: d.color });
    }
  }

  paneViews(): readonly IPrimitivePaneView[] {
    const placed = this.placed;
    const radius = this.diameter() / 2;
    return [{
      zOrder: () => 'top',
      renderer: () => ({
        draw: (target: DrawTarget) => {
          target.useBitmapCoordinateSpace(({ context, horizontalPixelRatio, verticalPixelRatio }) => {
            const r = radius * Math.max(horizontalPixelRatio, verticalPixelRatio);
            for (const p of placed) {
              context.beginPath();
              context.fillStyle = p.color;
              context.arc(
                p.x * horizontalPixelRatio, p.y * verticalPixelRatio,
                r, 0, 2 * Math.PI,
              );
              context.fill();
            }
          });
        },
      }),
    }];
  }

  hitTest(x: number, y: number): PrimitiveHoveredItem | null {
    const reach = Math.max(this.diameter() / 2, MIN_HIT_RADIUS);
    let best: { id: string; dist: number } | null = null;
    for (const p of this.placed) {
      const dist = Math.hypot(p.x - x, p.y - y);
      if (dist <= reach && (!best || dist < best.dist)) best = { id: p.id, dist };
    }
    if (!best) return null;
    return { externalId: best.id, zOrder: 'top', distance: best.dist, itemType: 'primitive' };
  }
}
