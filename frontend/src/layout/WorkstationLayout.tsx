import { useRef, useCallback } from 'react';
import { Layout, Model, Action } from 'flexlayout-react';
import { useStore } from '../data/store';
import { componentFactory } from './ComponentFactory';
import { variantA, variantB, variantC, variantD } from './variants';
import type { LayoutVariant } from '../types';

const STORAGE_PREFIX = 'ib-layout-model-';
const variantDefaults: Record<LayoutVariant, object> = {
  A: variantA, B: variantB, C: variantC, D: variantD,
};

function loadModel(variant: LayoutVariant): Model {
  try {
    const saved = localStorage.getItem(STORAGE_PREFIX + variant);
    if (saved) return Model.fromJson(JSON.parse(saved));
  } catch { /* corrupt data — fall through to default */ }
  return Model.fromJson(variantDefaults[variant]);
}

const variantModels: Record<string, Model> = {};

function getModel(variant: LayoutVariant): Model {
  if (!variantModels[variant]) {
    variantModels[variant] = loadModel(variant);
  }
  return variantModels[variant];
}

export function WorkstationLayout() {
  const activeVariant = useStore((s) => s.activeVariant);
  const prevVariant = useRef(activeVariant);

  // Reset model when variant changes
  if (prevVariant.current !== activeVariant) {
    // Force reload from localStorage or default for the new variant
    variantModels[activeVariant] = loadModel(activeVariant);
    prevVariant.current = activeVariant;
  }

  const model = getModel(activeVariant);

  const onAction = useCallback((action: Action) => {
    return action;
  }, []);

  const onModelChange = useCallback((m: Model) => {
    try {
      localStorage.setItem(STORAGE_PREFIX + activeVariant, JSON.stringify(m.toJson()));
    } catch { /* quota exceeded — ignore */ }
  }, [activeVariant]);

  return (
    <div className="flex-1 relative">
      <Layout
        model={model}
        factory={componentFactory}
        realtimeResize={true}
        onAction={onAction}
        onModelChange={onModelChange}
      />
    </div>
  );
}
