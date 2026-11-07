import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('jpeg');
Config.setOverwriteOutput(true);
// WSL has no sandbox-capable Chrome by default.
Config.setChromiumDisableWebSecurity(false);
Config.setConcurrency(2);
