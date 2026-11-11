/**
 * Note: When using the Node.JS APIs, the config file
 * doesn't apply. Instead, pass options directly to the APIs.
 *
 * All configuration options: https://remotion.dev/docs/config
 */

import { Config } from "@remotion/cli/config";

Config.setRspack(true);
Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
Config.setDefaultPremountInSeconds(2);
// The default headless shell never fires requestAnimationFrame on macOS 27, so rendering hangs while loading.
Config.setChromeMode("chrome-for-testing");
// One tab at a time. With several, Chrome sometimes hands back a half-painted frame (layers missing, tiles repeated),
// which shows as a one-frame glitch in the video. One tab was also no slower in tests.
Config.setConcurrency(1);
