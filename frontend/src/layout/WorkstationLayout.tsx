import { useRef, useEffect, useMemo } from 'react';
import { Layout, Model } from 'flexlayout-react';
import { useStore } from '../data/store';
import { componentFactory } from './ComponentFactory';
import { variantA, variantB, variantC, variantD } from './variants';
import type { LayoutVariant } from '../types';

const variantModels: Record<LayoutVariant, ReturnType<typeof Model.fromJson>> = {
  A: Model.fromJson(variantA),
  B: Model.fromJson(variantB),
  C: Model.fromJson(variantC),
  D: Model.fromJson(variantD),
};

export function WorkstationLayout() {
  const activeVariant = useStore((s) => s.activeVariant);
  const prevVariant = useRef(activeVariant);

  // Reset model when variant changes
  useEffect(() => {
    if (prevVariant.current !== activeVariant) {
      const configs = { A: variantA, B: variantB, C: variantC, D: variantD };
      variantModels[activeVariant] = Model.fromJson(configs[activeVariant]);
      prevVariant.current = activeVariant;
    }
  }, [activeVariant]);

  const model = variantModels[activeVariant];

  return (
    <div className="flex-1 relative">
      <Layout
        model={model}
        factory={componentFactory}
        realtimeResize={true}
      />
    </div>
  );
}
