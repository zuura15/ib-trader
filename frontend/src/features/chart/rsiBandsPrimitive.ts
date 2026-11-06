/**
 * Shaded RSI zones (overbought 70–80, oversold 20–30) drawn behind the
 * RSI line as a series primitive on the RSI series.
 */
import type {
  IPrimitivePaneRenderer, IPrimitivePaneView, ISeriesApi,
  ISeriesPrimitive, SeriesAttachedParameter, SeriesType, Time,
} from 'lightweight-charts';

export interface RsiBand {
  from: number;
  to: number;
  color: string;
}

type DrawTarget = Parameters<IPrimitivePaneRenderer['draw']>[0];

/** Fill opacity — faint enough that the RSI line and divergences read on top. */
const BAND_ALPHA = 0.14;

export class RsiBandsPrimitive implements ISeriesPrimitive<Time> {
  private series: ISeriesApi<SeriesType> | null = null;
  private bands: RsiBand[];
  private placed: Array<{ top: number; bottom: number; color: string }> = [];

  constructor(bands: RsiBand[]) {
    this.bands = bands;
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.series = param.series;
  }

  detached(): void {
    this.series = null;
  }

  updateAllViews(): void {
    const series = this.series;
    this.placed = [];
    if (!series) return;
    for (const b of this.bands) {
      const y1 = series.priceToCoordinate(b.from);
      const y2 = series.priceToCoordinate(b.to);
      if (y1 == null || y2 == null) continue;
      this.placed.push({ top: Math.min(y1, y2), bottom: Math.max(y1, y2), color: b.color });
    }
  }

  paneViews(): readonly IPrimitivePaneView[] {
    const placed = this.placed;
    return [{
      zOrder: () => 'bottom',
      renderer: () => ({
        draw: (target: DrawTarget) => {
          target.useBitmapCoordinateSpace(({ context, bitmapSize, verticalPixelRatio }) => {
            context.save();
            context.globalAlpha = BAND_ALPHA;
            for (const p of placed) {
              context.fillStyle = p.color;
              const top = Math.round(p.top * verticalPixelRatio);
              const bottom = Math.round(p.bottom * verticalPixelRatio);
              context.fillRect(0, top, bitmapSize.width, bottom - top);
            }
            context.restore();
          });
        },
      }),
    }];
  }
}
