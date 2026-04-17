/*
 *  This sketch sends random data over UDP on a ESP32 device
 */
#include <WiFi.h>
#include <WiFiUdp.h>

//定义引脚
#define POT1 34  // 只改这里：34 → POT1
#define POT2 35  // 只改这里：新增35

#define VCC_PIN2 21 // 给传感器供电的引脚
#define VCC_PIN1 22
//采集量
int pot_value;
int pot2_value;  // 只改这里：新增第二个变量

// 平均值滤波配置 - 可自由修改窗口大小
#define FILTER_WINDOW_SIZE 1
int filter_buffer[FILTER_WINDOW_SIZE];
int filter_index = 0;
bool filter_full = false;

int filter_buffer2[FILTER_WINDOW_SIZE];  // 滤波缓冲区
int filter_index2 = 0;                   
bool filter_full2 = false;                

// WiFi network name and password:
// const char * networkName = "fakeman";
// const char * networkPswd = "tong0707.ing";
const char * networkName = "Magic5 Pro";
const char * networkPswd = "12345678";


//IP address to send UDP data to:
// either use the ip address of the server or
// a network broadcast address
//const char * udpAddress = "10.105.245.68";//PC
const char * udpAddress = "10.105.245.224";//Board
const int udpPort = 8080;

//Are we currently connected?
boolean connected = false;

//The udp library class
WiFiUDP udp;

// 计算平均值滤波函数
int applyFilter(int newValue) {
  // 将新值存入缓冲区
  filter_buffer[filter_index] = newValue;
  filter_index++;
  
  // 检查缓冲区是否已满
  if(filter_index >= FILTER_WINDOW_SIZE) {
    filter_index = 0;
    filter_full = true;
  }
  
  // 计算平均值
  long sum = 0;
  int count = filter_full ? FILTER_WINDOW_SIZE : filter_index;
  
  for(int i = 0; i < count; i++) {
    sum += filter_buffer[i];
  }
  
  return sum / count;
}

// 只改这里：新增第二个滤波函数
int applyFilter2(int newValue) {
  filter_buffer2[filter_index2] = newValue;
  filter_index2++;
  
  if(filter_index2 >= FILTER_WINDOW_SIZE) {
    filter_index2 = 0;
    filter_full2 = true;
  }
  
  long sum = 0;
  int count = filter_full2 ? FILTER_WINDOW_SIZE : filter_index2;
  
  for(int i = 0; i < count; i++) {
    sum += filter_buffer2[i];
  }
  
  return sum / count;
}

void setup(){
      // 设置21引脚为输出模式，并输出高电平 (3.3V)
    pinMode(VCC_PIN1, OUTPUT);
    digitalWrite(VCC_PIN1, HIGH);
    pinMode(VCC_PIN2, OUTPUT);
    digitalWrite(VCC_PIN2, HIGH);
  // Initilize hardware serial:
  Serial.begin(9600);
      // ADC输入
  analogSetAttenuation(ADC_11db);
  pinMode(POT1, INPUT);  
  pinMode(POT2, INPUT);  
  //Connect to the WiFi network
  connectToWiFi(networkName, networkPswd);
}

void loop(){
  //读取模拟量
  pot_value = analogRead(POT1);   
  pot2_value = analogRead(POT2);  
  
  // 应用平均值滤波
  int filtered_value = applyFilter(pot_value);
  int filtered_value2 = applyFilter2(pot2_value);  
  
  // 打印格式 → 空格分隔
  Serial.print(filtered_value);
  Serial.print(" ");
  Serial.println(filtered_value2);
  
  //only send data when connected
  if(connected){
    //Send a packet
    udp.beginPacket(udpAddress,udpPort);
    udp.print(filtered_value);    
    udp.print(" ");               
    udp.println(filtered_value2); 
    udp.endPacket();
  }

  //Wait for 1 ms
  delay(1);
}

void connectToWiFi(const char * ssid, const char * pwd){
  Serial.println("Connecting to WiFi network: " + String(ssid));

  // delete old config
  WiFi.disconnect(true);
  //register event handler
  WiFi.onEvent(WiFiEvent);
  
  //Initiate connection
  WiFi.begin(ssid, pwd);

  Serial.println("Waiting for WIFI connection...");
}

//wifi event handler
void WiFiEvent(WiFiEvent_t event){
    switch(event) {
      case ARDUINO_EVENT_WIFI_STA_GOT_IP:
          //When connected set
          Serial.print("WiFi connected! IP address: ");
          Serial.println(WiFi.localIP());
          //initializes the UDP state
          //This initializes the transfer buffer
          udp.begin(WiFi.localIP(),udpPort);
          connected = true;
          break;
      case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
          Serial.println("WiFi lost connection");
          connected = false;
          break;
      default: break;
    }
}