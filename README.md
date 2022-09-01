# BlinkCameraAutoArm
Controls Blink camera state depending on particular ip list checks.

Application set Blink camera to armed state after several checks of selected ip addresses. If none of the addresses answers for amount attepmts camera is being set to armed state. When any of ip answers, camera sets to Disarmed.
Script runs using cron with helper startup bash script on Keenetic OPKG system.
