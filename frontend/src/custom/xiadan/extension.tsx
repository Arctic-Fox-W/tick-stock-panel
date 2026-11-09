import { lazy } from "react";
import { Wallet } from "lucide-react";
import type { FrontendExtension } from "@/extensions/types";

export const Xiadan = lazy(() =>
  import("./Page").then((m) => ({ default: m.Xiadan })),
);

const extension: FrontendExtension = {
  id: "xiadan.holdings",
  apiVersion: 1,
  routes: [{ id: "xiadan", path: "/xiadan", component: Xiadan }],
  navigation: [
    { id: "xiadan", routeId: "xiadan", label: "同花顺持仓", icon: Wallet },
  ],
};

export default extension;
