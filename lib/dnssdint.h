/**
 *  Copyright (C) 2012  Juho Vähä-Herttua
 *
 *  This library is free software; you can redistribute it and/or
 *  modify it under the terms of the GNU Lesser General Public
 *  License as published by the Free Software Foundation; either
 *  version 2.1 of the License, or (at your option) any later version.
 *
 *  This library is distributed in the hope that it will be useful,
 *  but WITHOUT ANY WARRANTY; without even the implied warranty of
 *  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 *  Lesser General Public License for more details.
 *
 *==================================================================
 * modified by fduncanh 2022
 */

#ifndef DNSSDINT_H
#define DNSSDINT_H

#include "global.h"

/* the previous behavior of announcing UxPlay with a fixed public key PK
 * can be restored by uncommenting the following line  */
//#define PK "b07727d6f6cd6e08b58ede525ec3cdeaa252ad9f683feb212ef8a205246554e7"

#define RAOP_TXTVERS "1"
#define RAOP_CH "2"             /* Audio channels: 2 */
#define RAOP_CN "0,1,2,3"       /* Audio codec: PCM, ALAC, AAC, AAC ELD */
#define RAOP_ET "0,3,5"         /* Encryption type: None, FairPlay, FairPlay SAPv2.5 */
#define RAOP_VV "2"
/* AirMirror (BoostVision, AppleTV3,2) 实测可被 macOS 识别为「可扩展屏」，此处复刻其 features。
 * 原 UxPlay 默认值（第一代 AppleTV3,2 镜像用）：
 *   FEATURES_1 "0x5A7FFEE6"  FEATURES_2 "0x0"
 * 复刻安卓 AirMirror 实测值：
 *   features = 0x4a7fdfd5, 0x400
 * 还原方法：把下面两行换回上面的值即可。 */
#define FEATURES_1 "0x4a7fdfd5" /* macOS 可见 AirMirror 实测值：低位 32bit */
#define FEATURES_2 "0x400"      /* macOS 可见 AirMirror 实测值：高位 32bit（bit 42） */
#define RAOP_RHD "5.6.0.0"
#define RAOP_SF "0x4"
#define RAOP_SV "false"
#define RAOP_DA "true"
#define RAOP_SR "44100"         /* Sample rate: 44100 */
#define RAOP_SS "16"            /* Sample size: 16 */
#define RAOP_VS GLOBAL_VERSION  /* defined in global.h */
#define RAOP_TP "UDP"           /* Transport protocol. Possible values: UDP or TCP or TCP,UDP */
#define RAOP_MD "0,1,2"         /* Metadata: text, artwork, progress */
#define RAOP_VN "65537"

#define AIRPLAY_SRCVERS GLOBAL_VERSION /*defined in global.h */
#define AIRPLAY_FLAGS "0x44"   /* AirMirror 实测 0x44（UxPlay 原值 0x84，差 bit7） */
#define AIRPLAY_VV "2"
#define AIRPLAY_PI "2e388006-13ba-4041-9a67-25dd4a43d536"

#endif
