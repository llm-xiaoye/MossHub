import React from "react";
import {createRoot} from "react-dom/client";
import {HashRouter} from "react-router-dom";
import {App as AntApp,ConfigProvider,theme} from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import "./style.css";
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{
      algorithm:theme.darkAlgorithm,
      token:{
        colorPrimary:"#2F7D5B",colorPrimaryHover:"#3D966B",colorPrimaryActive:"#256247",
        colorLink:"#7BCEA3",colorLinkHover:"#A2E4BF",colorInfo:"#7BCEA3",
        colorBgBase:"#0B1712",colorBgLayout:"#0B1712",colorBgContainer:"#13251C",colorBgElevated:"#13251C",
        colorText:"#E7F0EA",colorTextSecondary:"#A7B9AC",colorTextPlaceholder:"#8DA595",colorTextDisabled:"#819488",
        colorBorder:"#2A4637",colorBorderSecondary:"#2A4637",controlItemBgHover:"#1B3327",controlItemBgActive:"#1B3327",
        colorSuccess:"#73C99B",colorWarning:"#EAC56B",colorError:"#FF8B87",
        borderRadius:8,fontFamily:"-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif",fontSize:14,
      },
      components:{Button:{primaryColor:"#FFFFFF"},Table:{headerBg:"#1B3327",rowHoverBg:"#1B3327"},Tree:{nodeHoverBg:"#1B3327",nodeSelectedBg:"#1B3327"}},
    }}><AntApp><HashRouter><App/></HashRouter></AntApp></ConfigProvider>
  </React.StrictMode>
);
