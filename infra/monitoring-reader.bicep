targetScope = 'resourceGroup'

@description('Name of the Application Insights component.')
param applicationInsightsName string

@description('Object ID of the managed identity that reads Application Insights data.')
param principalId string

var monitoringReaderRoleDefinitionGuid = '43d0d8ad-25c7-4714-9337-8ba259a9fe05'

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

resource monitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(applicationInsights.id, principalId, monitoringReaderRoleDefinitionGuid)
  scope: applicationInsights
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringReaderRoleDefinitionGuid)
  }
}
